"""Concurrent scheduler round and specialist lane execution."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ctf_agent.frontier import AdaptiveFrontier, FrontierLaneId
from ctf_agent.schemas import FlagCandidate, Hypothesis, SpecialistResult
from ctf_agent.specialists.base import SliceSpecialist, Specialist

if TYPE_CHECKING:
    from ctf_agent.scheduler import Scheduler


@runtime_checkable
class CandidateVerifier(Protocol):
    async def __call__(self, candidate: FlagCandidate) -> bool: ...


@dataclass(frozen=True, slots=True)
class WaveResult:
    results: tuple[SpecialistResult, ...]
    verified: bool
    provisional: bool


async def run_round(
    scheduler: Scheduler,
    hypotheses: tuple[Hypothesis, ...],
    context: dict[str, object],
) -> list[SpecialistResult]:
    frontier = AdaptiveFrontier(hypotheses, active_width=scheduler.max_concurrency)
    for lane_id in frontier.next_lane_ids():
        frontier.admit(lane_id)
    return list((await run_wave(scheduler, hypotheses, context, frontier)).results)


async def run_wave(
    scheduler: Scheduler,
    hypotheses: tuple[Hypothesis, ...],
    context: dict[str, object],
    frontier: AdaptiveFrontier,
) -> WaveResult:
    from ctf_agent.scheduler_frontier import emit_frontier

    tasks: list[asyncio.Task[SpecialistResult]] = []
    task_lanes: dict[asyncio.Task[SpecialistResult], FrontierLaneId] = {}
    semaphore = asyncio.Semaphore(scheduler.max_concurrency)
    for hypothesis in hypotheses:
        matching = [
            specialist
            for specialist in scheduler.specialists
            if specialist.supports(hypothesis.claim)
        ]
        selected = matching or list(scheduler.specialists)
        for specialist in selected:
            task = asyncio.create_task(
                solve_lane_limited(semaphore, specialist, hypothesis, context)
            )
            tasks.append(task)
            task_lanes[task] = FrontierLaneId(hypothesis.id)

    verifier_value = context.get("candidate_verifier")
    verifier = verifier_value if isinstance(verifier_value, CandidateVerifier) else None
    provisional_mode = context.get("provisional_candidates") is True
    results: list[SpecialistResult] = []
    pending = set(tasks)
    verified = False
    provisional = False
    while pending:
        done, pending = await asyncio.wait(
            pending, return_when=asyncio.FIRST_COMPLETED
        )
        for task in sorted(done, key=tasks.index):
            result = task.result()
            lane_id = task_lanes[task]
            accepted: list[FlagCandidate] = []
            for candidate in result.flag_candidates:
                candidate_hash = hashlib.sha256(candidate.value.encode()).hexdigest()
                frontier.provisional(lane_id, candidate_hash)
                if verifier is not None:
                    if await verifier(candidate):
                        frontier.verify(lane_id, candidate_hash)
                        accepted.append(candidate)
                        verified = True
                    else:
                        frontier.reject(lane_id, candidate_hash)
                elif provisional_mode:
                    provisional = True
                else:
                    frontier.verify(lane_id, candidate_hash)
                    accepted.append(candidate)
                    verified = True
            if result.flag_candidates and not accepted and not provisional_mode:
                result = result.model_copy(
                    update={"status": "inconclusive", "flag_candidates": []}
                )
            elif accepted and accepted != result.flag_candidates:
                result = result.model_copy(update={"flag_candidates": accepted})
            results.append(result)
            emit_frontier(context, frontier)
        if verified:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            break
    return WaveResult(tuple(results), verified, provisional)


async def solve_lane_limited(
    semaphore: asyncio.Semaphore,
    specialist: Specialist,
    hypothesis: Hypothesis,
    context: dict[str, object],
) -> SpecialistResult:
    async with semaphore:
        return await solve_lane(specialist, hypothesis, context)


async def solve_lane(
    specialist: Specialist,
    hypothesis: Hypothesis,
    context: dict[str, object],
) -> SpecialistResult:
    try:
        if isinstance(specialist, SliceSpecialist):
            configured_steps = context.get("lane_slice_max_steps", 2)
            max_steps = configured_steps if isinstance(configured_steps, int) else 2
            outcome = await specialist.run_slice(
                hypothesis, context, max_steps=max_steps
            )
            statuses = context.setdefault("_lane_slice_statuses", [])
            if isinstance(statuses, list):
                statuses.append(outcome.status.value)
            by_hypothesis = context.setdefault(
                "_lane_slice_status_by_hypothesis", {}
            )
            if isinstance(by_hypothesis, dict):
                by_hypothesis[hypothesis.id] = outcome.status.value
            return outcome.specialist_result
        return await specialist.solve(hypothesis, context)
    except Exception as exc:
        return SpecialistResult(
            hypothesis_id=hypothesis.id,
            status="inconclusive",
            next_action=f"{specialist.name} failed with {type(exc).__name__}: {exc}",
            confidence=0.0,
        )
