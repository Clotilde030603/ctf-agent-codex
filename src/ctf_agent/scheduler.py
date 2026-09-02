"""Public scheduler API backed by frontier and round executors."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ctf_agent.frontier import AdaptiveFrontier, FrontierLaneId
from ctf_agent.planning import MAX_ACTIVE_HYPOTHESES, HypothesisPlanner
from ctf_agent.planning import ModelHypothesisPlanner as ModelHypothesisPlanner
from ctf_agent.planning import StaticHypothesisPlanner as StaticHypothesisPlanner
from ctf_agent.planning import hypothesis_from_mapping as hypothesis_from_mapping
from ctf_agent.scheduler_frontier import (
    aggregate_lane_results,
    emit_frontier,
    retire_terminal_lane,
    run_scheduler,
)
from ctf_agent.scheduler_round import (
    CandidateVerifier,
    WaveResult,
    run_round,
    run_wave,
    solve_lane,
    solve_lane_limited,
)
from ctf_agent.schemas import Hypothesis, SpecialistResult
from ctf_agent.specialists.base import Specialist

MAX_HYPOTHESES = MAX_ACTIVE_HYPOTHESES


@dataclass(frozen=True)
class SchedulerRunResult:
    hypotheses: tuple[Hypothesis, ...]
    specialist_results: tuple[SpecialistResult, ...]
    solved: bool
    stop_reason: str

    @property
    def accepted_flags(self) -> tuple[str, ...]:
        flags: list[str] = []
        for result in self.specialist_results:
            if result.status == "confirmed":
                flags.extend(candidate.value for candidate in result.flag_candidates)
        return tuple(dict.fromkeys(flags))


@dataclass
class Scheduler:
    planner: HypothesisPlanner
    specialists: tuple[Specialist, ...]
    no_progress_cutoff: int = 1
    max_rounds: int = 1
    max_concurrency: int = MAX_HYPOTHESES
    adaptive_frontier: bool = True

    def __post_init__(self) -> None:
        if self.max_concurrency < 1 or self.max_concurrency > MAX_HYPOTHESES:
            raise ValueError("max_concurrency must be between 1 and 3")

    async def run(self, context: dict[str, object]) -> SchedulerRunResult:
        return await run_scheduler(self, context)

    async def _run_round(
        self,
        hypotheses: tuple[Hypothesis, ...],
        context: dict[str, object],
    ) -> list[SpecialistResult]:
        return await run_round(self, hypotheses, context)

    async def _run_wave(
        self,
        hypotheses: tuple[Hypothesis, ...],
        context: dict[str, object],
        frontier: AdaptiveFrontier,
    ) -> WaveResult:
        return await run_wave(self, hypotheses, context, frontier)

    @staticmethod
    def _retire_terminal_lane(
        context: dict[str, object],
        frontier: AdaptiveFrontier,
        lane_id: FrontierLaneId,
        results: list[SpecialistResult],
    ) -> None:
        retire_terminal_lane(context, frontier, lane_id, results)

    @staticmethod
    def _emit_frontier(
        context: dict[str, object], frontier: AdaptiveFrontier
    ) -> None:
        emit_frontier(context, frontier)

    @classmethod
    async def _solve_lane_limited(
        cls,
        semaphore: asyncio.Semaphore,
        specialist: Specialist,
        hypothesis: Hypothesis,
        context: dict[str, object],
    ) -> SpecialistResult:
        return await solve_lane_limited(semaphore, specialist, hypothesis, context)

    @staticmethod
    async def _solve_lane(
        specialist: Specialist,
        hypothesis: Hypothesis,
        context: dict[str, object],
    ) -> SpecialistResult:
        return await solve_lane(specialist, hypothesis, context)


_aggregate_lane_results = aggregate_lane_results

__all__ = [
    "MAX_HYPOTHESES",
    "CandidateVerifier",
    "ModelHypothesisPlanner",
    "Scheduler",
    "SchedulerRunResult",
    "StaticHypothesisPlanner",
    "WaveResult",
    "hypothesis_from_mapping",
]
