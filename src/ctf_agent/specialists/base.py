from __future__ import annotations

from typing import Protocol, runtime_checkable

from ctf_agent.lanes.model import LaneRunResult
from ctf_agent.schemas import Hypothesis, SpecialistResult


def progress_made(result: SpecialistResult) -> bool:
    return bool(
        result.status == "confirmed"
        or result.facts
        or result.flag_candidates
        or result.artifacts
        or result.commands
        or result.reproduction_command
    )


@runtime_checkable
@runtime_checkable
class SliceSpecialist(Protocol):
    name: str

    async def run_slice(
        self,
        hypothesis: Hypothesis,
        context: dict[str, object],
        *,
        max_steps: int | None = None,
    ) -> LaneRunResult: ...


@runtime_checkable
class Specialist(Protocol):
    name: str

    def supports(self, category: str) -> bool:
        """Return whether this specialist should handle a planned hypothesis."""
        ...

    async def solve(self, hypothesis: Hypothesis, context: dict[str, object]) -> SpecialistResult:
        """Run one independent solving lane for a planned hypothesis."""
        ...
