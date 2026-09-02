"""Blind model reviewer that re-derives flags without receiving the solver candidate."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from ctf_agent.config import Settings
from ctf_agent.models.base import ModelBackend, ModelBackendError, ModelRequest
from ctf_agent.models.factory import create_codex_backend
from ctf_agent.skills import SkillSelection

ReviewerBackendFactory = Callable[[Settings, str, Path], ModelBackend]


class ModelReviewFinding(BaseModel):
    candidate: str
    source_artifact: str
    source_location: str
    reproduction_command: str
    evidence: list[str] = Field(default_factory=list)


class ModelReviewResponse(BaseModel):
    findings: list[ModelReviewFinding] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ModelReviewOutcome:
    accepted: bool
    reason: str
    findings: tuple[ModelReviewFinding, ...] = ()
    facts: tuple[str, ...] = ()

    @property
    def derived_candidates(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(finding.candidate for finding in self.findings))


@dataclass(slots=True)
class ModelBlindReviewer:
    settings: Settings
    run_dir: Path
    flag_policy: dict[str, object]
    backend_factory: ReviewerBackendFactory = create_codex_backend
    skills: SkillSelection | None = None

    async def derive(self) -> ModelReviewOutcome:
        solver = self.run_dir / "solve.py"
        if not solver.is_file():
            return ModelReviewOutcome(False, "reviewer solver file is missing")
        with tempfile.TemporaryDirectory(prefix="ctf-model-review-") as temp_dir:
            review_dir = Path(temp_dir)
            shutil.copy2(solver, review_dir / "solve.py")
            files = self.run_dir / "files"
            if files.is_dir():
                _copy_regular_tree(files, review_dir / "files")
            inventory = _file_inventory(review_dir / "files")
            backend = (
                create_codex_backend(
                    self.settings,
                    "reviewer",
                    review_dir,
                    projection_run_dir=self.run_dir,
                )
                if self.backend_factory is create_codex_backend
                else self.backend_factory(self.settings, "reviewer", review_dir)
            )
            try:
                response = await backend.complete(
                    ModelRequest(
                        role="reviewer",
                        system=(
                            "Blindly verify an authorized CTF solver. The expected candidate is "
                            "not provided. Inspect original files and solve.py, run the solver if "
                            "needed, and return only independently derived candidates."
                        ),
                        prompt=(
                            "Derive every flag matching the supplied policy from files/ using "
                            "solve.py. Do not infer an expected value from metadata. For every "
                            "candidate return its source artifact, source location, reproduction "
                            "command, and evidence."
                        ),
                        context={
                            "flag_policy": self.flag_policy,
                            "solver": "solve.py",
                            "files": inventory,
                        },
                        output_schema=ModelReviewResponse.model_json_schema(),
                        developer=(
                            self.skills.developer_instructions
                            if self.skills is not None
                            else None
                        ),
                        skill_runtime=(
                            self.skills.runtime if self.skills is not None else None
                        ),
                    )
                )
            except ModelBackendError as exc:
                return ModelReviewOutcome(
                    False,
                    f"reviewer backend failed: {type(exc).__name__}: {exc}",
                )
        try:
            payload = ModelReviewResponse.model_validate(json.loads(response.content))
        except (json.JSONDecodeError, ValueError) as exc:
            return ModelReviewOutcome(False, f"reviewer response invalid: {exc}")
        findings = tuple(
            finding
            for finding in payload.findings
            if _valid_finding(finding, self.run_dir)
        )
        if not findings:
            return ModelReviewOutcome(
                False,
                "reviewer independently derived no provenance-backed candidates",
            )
        return ModelReviewOutcome(
            True,
            "reviewer independently derived provenance-backed candidate set",
            findings,
            tuple(fact for finding in findings for fact in finding.evidence),
        )


def _valid_finding(finding: ModelReviewFinding, run_dir: Path) -> bool:
    artifact = (run_dir / finding.source_artifact).resolve()
    if run_dir.resolve() not in artifact.parents or not artifact.is_file():
        return False
    return finding.reproduction_command.strip() in {
        "python solve.py",
        "python3 solve.py",
    } and bool(finding.source_location.strip()) and bool(finding.evidence)


def _copy_regular_tree(source_root: Path, target_root: Path) -> None:
    for source in source_root.rglob("*"):
        if source.is_symlink() or not source.is_file():
            continue
        relative = source.relative_to(source_root)
        target = target_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _file_inventory(root: Path) -> list[dict[str, object]]:
    if not root.is_dir():
        return []
    output: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        output.append(
            {
                "path": path.relative_to(root.parent).as_posix(),
                "size": path.stat().st_size,
                "sha256": digest,
            }
        )
    return output
