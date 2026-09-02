"""Typed pwn/reverse-engineering toolchain harnesses for model-backed lanes."""

from __future__ import annotations

from dataclasses import dataclass

from ctf_agent.capabilities import CapabilityStatus, RuntimeCapabilitySnapshot
from ctf_agent.schemas import Hypothesis, SpecialistResult


@dataclass(frozen=True, slots=True)
class ToolRequirement:
    command: str
    install: str
    required: bool = False
    python_module: str | None = None


@dataclass(frozen=True, slots=True)
class ToolchainProfile:
    category: str
    requirements: tuple[ToolRequirement, ...]
    fallback: str


REV_PROFILE = ToolchainProfile(
    category="rev",
    requirements=(
        ToolRequirement("file", "versioned CTF tool image", required=True),
        ToolRequirement("strings", "GNU binutils", required=True),
        ToolRequirement("objdump", "GNU binutils", required=True),
        ToolRequirement("readelf", "GNU binutils", required=True),
        ToolRequirement("rizin", "install rizin from its official package"),
        ToolRequirement("ghidra", "install Ghidra headless and add it to PATH"),
        ToolRequirement("angr", "pip install angr", python_module="angr"),
    ),
    fallback="use binutils strings/headers and the isolated model worker",
)

PWN_PROFILE = ToolchainProfile(
    category="pwn",
    requirements=(
        ToolRequirement("file", "versioned CTF tool image", required=True),
        ToolRequirement("strings", "GNU binutils", required=True),
        ToolRequirement("checksec", "versioned CTF tool image", required=True),
        ToolRequirement("gdb", "install GDB in a dedicated pwn image"),
        ToolRequirement("ROPgadget", "pip install ROPgadget"),
        ToolRequirement("pwntools", "pip install pwntools", python_module="pwn"),
    ),
    fallback="use checksec/binutils observations and the isolated model worker",
)


class ToolchainSpecialist:
    def __init__(
        self,
        profile: ToolchainProfile,
        runtime_capabilities: RuntimeCapabilitySnapshot,
    ) -> None:
        self.profile = profile
        self.runtime_capabilities = runtime_capabilities
        self.name = f"{profile.category}-toolchain"

    def supports(self, category: str) -> bool:
        lowered = category.lower()
        aliases = (
            {"rev", "reverse", "binary"}
            if self.profile.category == "rev"
            else {"pwn", "heap", "rop"}
        )
        return any(alias in lowered for alias in aliases)

    async def solve(
        self, hypothesis: Hypothesis, context: dict[str, object]
    ) -> SpecialistResult:
        available: list[str] = []
        missing: list[str] = []
        required_missing: list[ToolRequirement] = []
        for requirement in self.profile.requirements:
            capability = self.runtime_capabilities.require(requirement.command)
            if capability.status is CapabilityStatus.AVAILABLE:
                version = capability.version or "unknown"
                available.append(
                    f"tool available: {requirement.command}; version={version}"
                )
                continue
            importance = "required" if requirement.required else "optional"
            missing.append(
                f"dependency unavailable ({importance}): {requirement.command}; "
                f"status={capability.status.value}; reason={capability.reason}; "
                f"{requirement.install}"
            )
            if requirement.required:
                required_missing.append(requirement)

        observations, artifacts = _triage_observations(context, self.profile)
        stopped = bool(required_missing)
        return SpecialistResult(
            hypothesis_id=hypothesis.id,
            status="inconclusive",
            facts=[
                f"typed {self.profile.category} toolchain profile selected",
                *available,
                *missing,
                *observations,
                f"lane stopped: {str(stopped).lower()}",
            ],
            artifacts=artifacts,
            next_action=(
                "install required dependencies before dynamic analysis"
                if stopped
                else self.profile.fallback
            ),
            confidence=0.25 if observations and not stopped else 0.05,
        )


def _triage_observations(
    context: dict[str, object], profile: ToolchainProfile
) -> tuple[list[str], list[str]]:
    triage = context.get("triage")
    if not isinstance(triage, dict):
        return [], []
    facts: list[str] = []
    artifacts: list[str] = []
    tools = {requirement.command.casefold() for requirement in profile.requirements}
    files = triage.get("files")
    if not isinstance(files, list):
        return [], []
    for item in files:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or item.get("relative_path") or "unknown")
        magic = str(item.get("magic") or "")
        if "elf" in magic.casefold() or "pe executable" in magic.casefold():
            facts.append(f"native binary triage target: {path}; magic={magic}")
        results = item.get("tool_results")
        if not isinstance(results, list):
            continue
        for result in results:
            if not isinstance(result, dict):
                continue
            tool = str(result.get("tool") or "")
            if tool.casefold() not in tools or result.get("missing") is True:
                continue
            facts.append(
                f"triage observation available from {tool} for {path}; "
                f"exit_code={result.get('exit_code')}"
            )
            artifact = result.get("stdout_artifact")
            if isinstance(artifact, str) and artifact not in artifacts:
                artifacts.append(artifact)
    return facts, artifacts
