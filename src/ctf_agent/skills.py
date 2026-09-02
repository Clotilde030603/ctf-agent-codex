"""Trusted, category-routed skill registry for model runtime instructions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final

import click
import typer
from pydantic import BaseModel, ConfigDict

from ctf_agent.security import secure_write_json

if TYPE_CHECKING:
    from ctf_agent.capabilities import RuntimeCapabilitySnapshot


class RuntimeMode(StrEnum):
    INJECTED = "injected"
    REFERENCE_ONLY = "reference_only"


class SkillIdentity(BaseModel):
    """Serializable identity for one trusted skill revision."""

    model_config = ConfigDict(frozen=True)

    skill_id: str
    sha256: str
    runtime_mode: RuntimeMode


class ToolRouting(BaseModel):
    model_config = ConfigDict(frozen=True)

    category: str
    planner_skill_ids: tuple[str, ...]
    solver_skill_ids: tuple[str, ...]
    verifier_skill_ids: tuple[str, ...]
    allowed_actions: tuple[str, ...]


class SkillRuntime(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = 1
    identities: tuple[SkillIdentity, ...]
    tool_routing: ToolRouting

    @property
    def selected_ids(self) -> tuple[str, ...]:
        return tuple(item.skill_id for item in self.identities)


class RuntimeSkillArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = 1
    selected_skills: tuple[SkillIdentity, ...]
    tool_routing: ToolRouting


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    skill_id: str
    category: str
    path: Path
    sha256: str
    runtime_mode: RuntimeMode


@dataclass(frozen=True, slots=True)
class SkillSelection:
    skills: tuple[SkillDefinition, ...]
    runtime: SkillRuntime
    developer_instructions: str

    @property
    def identities(self) -> tuple[SkillIdentity, ...]:
        return self.runtime.identities

    @property
    def tool_routing(self) -> ToolRouting:
        return self.runtime.tool_routing

    @property
    def selected_ids(self) -> tuple[str, ...]:
        return self.runtime.selected_ids

    @property
    def injected_skills(self) -> tuple[SkillDefinition, ...]:
        return tuple(
            item for item in self.skills if item.runtime_mode is RuntimeMode.INJECTED
        )

    @property
    def injected_ids(self) -> tuple[str, ...]:
        return tuple(item.skill_id for item in self.injected_skills)

    def write(self, path: Path) -> None:
        artifact = RuntimeSkillArtifact(
            selected_skills=self.identities,
            tool_routing=self.tool_routing,
        )
        secure_write_json(path, artifact.model_dump(mode="json"))


@dataclass(frozen=True, slots=True)
class UnknownSkillError(LookupError):
    skill_id: str

    def __str__(self) -> str:
        return f"skill is not declared in the trusted registry: {self.skill_id}"


@dataclass(frozen=True, slots=True)
class UntrustedSkillPathError(ValueError):
    path: Path

    def __str__(self) -> str:
        return f"skill path is outside the trusted registry: {self.path}"


class SkillCapabilityMismatchError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)

    def __str__(self) -> str:
        return f"trusted skill runtime capability mismatch: {self.reason}"


_SKILL_CATEGORIES: Final[tuple[tuple[str, str, RuntimeMode], ...]] = (
    ("ctf-core", "core", RuntimeMode.INJECTED),
    ("ctf-crypto-binary", "crypto-binary", RuntimeMode.INJECTED),
    ("ctf-crypto-math", "crypto-math", RuntimeMode.INJECTED),
    ("ctf-forensics", "forensics", RuntimeMode.INJECTED),
    ("ctf-pwn", "pwn", RuntimeMode.INJECTED),
    ("ctf-rev", "rev", RuntimeMode.INJECTED),
    ("ctf-web", "web", RuntimeMode.INJECTED),
    ("ctf-writeup", "writeup", RuntimeMode.REFERENCE_ONLY),
)
_CATEGORY_ALIASES: Final = {"crypto": "crypto-binary", "reverse": "rev"}
_ALLOWED_ACTIONS: Final = ("run", "write_file", "http_request", "finish")


class SkillRegistry:
    """Registry that accepts instruction content only from trusted skill assets."""

    def __init__(self) -> None:
        source_root = (Path(__file__).resolve().parents[2] / "skills").resolve()
        packaged_root = (Path(__file__).resolve().parent / "skills").resolve()
        trusted_root = source_root if source_root.is_dir() else packaged_root
        self.trusted_root = trusted_root.resolve(strict=True)
        self._definitions = tuple(
            self._load_definition(skill_id, category, runtime_mode)
            for skill_id, category, runtime_mode in _SKILL_CATEGORIES
        )
        self._by_id = {item.skill_id: item for item in self._definitions}

    @classmethod
    def repository(cls) -> SkillRegistry:
        return cls()

    @property
    def definitions(self) -> tuple[SkillDefinition, ...]:
        return self._definitions

    @property
    def digest(self) -> str:
        payload = [
            {
                "skill_id": item.skill_id,
                "sha256": item.sha256,
                "runtime_mode": item.runtime_mode.value,
            }
            for item in self._definitions
        ]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def definition(self, skill_id: str) -> SkillDefinition:
        try:
            return self._by_id[skill_id]
        except KeyError as exc:
            raise UnknownSkillError(skill_id) from exc

    def definition_for_path(self, path: Path) -> SkillDefinition:
        resolved = path.resolve()
        for definition in self._definitions:
            if definition.path == resolved:
                return definition
        raise UntrustedSkillPathError(resolved)

    def select(
        self,
        category: str,
        *,
        runtime_capabilities: RuntimeCapabilitySnapshot | None = None,
    ) -> SkillSelection:
        if runtime_capabilities is not None:
            from ctf_agent.capabilities import CapabilityStatus

            capability = runtime_capabilities.require("skill:trusted-runtime")
            if capability.status is not CapabilityStatus.AVAILABLE:
                raise SkillCapabilityMismatchError(capability.reason)
            if capability.digest != self.digest:
                raise SkillCapabilityMismatchError("registry digest drift")
        normalized = _CATEGORY_ALIASES.get(category.strip().lower(), category.strip().lower())
        skills = tuple(
            item
            for item in self._definitions
            if item.runtime_mode is RuntimeMode.INJECTED
            and item.category in {"core", normalized}
        )
        identities = tuple(
            SkillIdentity(
                skill_id=item.skill_id,
                sha256=item.sha256,
                runtime_mode=item.runtime_mode,
            )
            for item in skills
        )
        skill_ids = tuple(item.skill_id for item in skills)
        runtime = SkillRuntime(
            identities=identities,
            tool_routing=ToolRouting(
                category=normalized,
                planner_skill_ids=skill_ids,
                solver_skill_ids=skill_ids,
                verifier_skill_ids=skill_ids,
                allowed_actions=_ALLOWED_ACTIONS,
            ),
        )
        return SkillSelection(
            skills=skills,
            runtime=runtime,
            developer_instructions=self._render_instructions(skills),
        )

    def _load_definition(
        self, skill_id: str, category: str, runtime_mode: RuntimeMode
    ) -> SkillDefinition:
        path = (self.trusted_root / skill_id / "SKILL.md").resolve(strict=True)
        try:
            path.relative_to(self.trusted_root)
        except ValueError as exc:
            raise UntrustedSkillPathError(path) from exc
        return SkillDefinition(
            skill_id=skill_id,
            category=category,
            path=path,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            runtime_mode=runtime_mode,
        )

    @staticmethod
    def _render_instructions(skills: tuple[SkillDefinition, ...]) -> str:
        sections = ["<trusted-skill-runtime schema-version=\"1\">"]
        for skill in skills:
            sections.extend(
                (
                    f'<skill id="{skill.skill_id}" sha256="{skill.sha256}" '
                    f'mode="{skill.runtime_mode.value}">',
                    skill.path.read_text(encoding="utf-8"),
                    "</skill>",
                )
            )
        sections.append("</trusted-skill-runtime>")
        return "\n".join(sections)


app = typer.Typer(add_completion=False, help="Inspect trusted runtime skills.")


@app.command()
def main(
    category: str | None = typer.Option(..., "--category"),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Emit the deterministic trusted selection for one category."""
    if category is None:
        raise click.UsageError("Missing option '--category'.")
    selection = SkillRegistry.repository().select(category)
    payload = [item.model_dump(mode="json") for item in selection.identities]
    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True))
        return
    for item in payload:
        typer.echo(f"{item['skill_id']} {item['runtime_mode']} {item['sha256']}")


if __name__ == "__main__":
    app()
