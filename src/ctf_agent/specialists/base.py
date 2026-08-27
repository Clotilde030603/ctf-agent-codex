from __future__ import annotations

from typing import Protocol, runtime_checkable

from ctf_agent.schemas import Hypothesis, SpecialistResult


def progress_made(result: SpecialistResult) -> bool:
    return bool(
        result.status == "confirmed"
        or result.flag_candidates
        or result.artifacts
        or result.commands
        or result.reproduction_command
        or result.next_action
    )


@runtime_checkable
class Specialist(Protocol):
    name: str

    def supports(self, category: str) -> bool:
        """Return whether this specialist should handle a planned hypothesis."""
        ...

    async def solve(self, hypothesis: Hypothesis, context: dict[str, object]) -> SpecialistResult:
        """Run one independent solving lane for a planned hypothesis."""
        ...
