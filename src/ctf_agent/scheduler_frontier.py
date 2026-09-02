"""Adaptive frontier scheduling and lane-state transitions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from ctf_agent.budget_types import ProgressEvidence
from ctf_agent.frontier import AdaptiveFrontier, FrontierLaneId
from ctf_agent.frontier.recovery import restore_frontier
from ctf_agent.schemas import SpecialistResult

if TYPE_CHECKING:
    from ctf_agent.scheduler import Scheduler, SchedulerRunResult


async def run_scheduler(
    scheduler: Scheduler, context: dict[str, object]
) -> SchedulerRunResult:
    from ctf_agent.scheduler import SchedulerRunResult

    hypotheses = await scheduler.planner.plan(context)
    if not scheduler.specialists:
        return SchedulerRunResult(hypotheses, (), False, "no_specialists")
    if not scheduler.adaptive_frontier:
        fixed_results: list[SpecialistResult] = []
        for _ in range(scheduler.max_rounds):
            fixed_results.extend(await scheduler._run_round(hypotheses, context))
            if any(item.flag_candidates for item in fixed_results):
                break
        solved = any(
            item.status == "confirmed" and item.flag_candidates
            for item in fixed_results
        )
        return SchedulerRunResult(
            hypotheses,
            tuple(fixed_results),
            solved,
            "solved" if solved else "max_rounds",
        )
    raw_frontier_events = context.get("frontier_events")
    frontier_events = (
        tuple(item for item in raw_frontier_events if isinstance(item, Mapping))
        if isinstance(raw_frontier_events, list)
        else ()
    )
    frontier = (
        restore_frontier(
            hypotheses,
            frontier_events,
            active_width=scheduler.max_concurrency,
        )
        if frontier_events
        else AdaptiveFrontier(
            hypotheses,
            active_width=scheduler.max_concurrency,
            total_pool_cap=len(hypotheses),
        )
    )
    emit_frontier(context, frontier)
    by_id = {hypothesis.id: hypothesis for hypothesis in hypotheses}
    results: list[SpecialistResult] = []
    made_progress = False
    stop_reason = "max_rounds"
    replacements_remaining = scheduler.max_rounds

    def observe_results(
        lane_id: FrontierLaneId, lane_results: list[SpecialistResult]
    ) -> bool:
        provider = context.get("progress_evidence_provider")
        supplied = provider(tuple(lane_results)) if callable(provider) else ProgressEvidence()
        evidence = supplied if isinstance(supplied, ProgressEvidence) else ProgressEvidence()
        previous_novelty = next(
            lane.score.novelty for lane in frontier.lanes if lane.lane_id == lane_id
        )
        observed = frontier.observe(lane_id, evidence)
        novel = observed.score.novelty > previous_novelty
        decider = context.get("budget_extension_decider")
        if novel and callable(decider):
            decider(evidence)
        return novel

    async def replace_retired(lane_id: FrontierLaneId) -> None:
        nonlocal replacements_remaining
        if replacements_remaining <= 0:
            return
        replacements_remaining -= 1
        replanned = await scheduler.planner.plan(context)
        for hypothesis in replanned:
            replacement = frontier.replace(lane_id, hypothesis)
            if replacement is None:
                continue
            by_id[str(replacement)] = hypothesis.model_copy(
                update={"id": str(replacement)}
            )
            return

    while any(lane.quanta < frontier.minimum_quantum for lane in frontier.lanes):
        lane_ids = frontier.next_lane_ids()
        if not lane_ids:
            break
        for lane_id in lane_ids:
            frontier.admit(lane_id)
        emit_frontier(context, frontier)
        wave = await scheduler._run_wave(
            tuple(by_id[str(lane_id)] for lane_id in lane_ids), context, frontier
        )
        results.extend(wave.results)
        for lane_id in lane_ids:
            lane_results = [
                item for item in wave.results if item.hypothesis_id == str(lane_id)
            ]
            novel = observe_results(lane_id, lane_results)
            made_progress = made_progress or novel
            if retire_terminal_lane(context, frontier, lane_id, lane_results, novel):
                await replace_retired(lane_id)
        emit_frontier(context, frontier)
        if wave.verified:
            stop_reason = "solved"
            break
        if wave.provisional:
            frontier.pause_active()
            emit_frontier(context, frontier)
            stop_reason = "candidate_provisional"
            break

    if stop_reason == "max_rounds" and not made_progress and not frontier_events:
        stop_reason = "no_progress"
    extra_quanta = max(0, scheduler.max_rounds - 1) * scheduler.max_concurrency
    while stop_reason == "max_rounds" and extra_quanta > 0:
        lane_ids = frontier.next_lane_ids(limit=1)
        if not lane_ids:
            break
        lane_id = lane_ids[0]
        frontier.admit(lane_id)
        emit_frontier(context, frontier)
        wave = await scheduler._run_wave((by_id[str(lane_id)],), context, frontier)
        results.extend(wave.results)
        lane_results = list(wave.results)
        novel = observe_results(lane_id, lane_results)
        made_progress = made_progress or novel
        if retire_terminal_lane(context, frontier, lane_id, lane_results, novel):
            await replace_retired(lane_id)
        emit_frontier(context, frontier)
        extra_quanta -= 1
        if wave.verified:
            stop_reason = "solved"
        elif wave.provisional:
            stop_reason = "candidate_provisional"

    if stop_reason == "max_rounds" and not made_progress:
        stop_reason = "no_progress"
    lane_statuses = context.get("_lane_slice_statuses", [])
    if stop_reason == "max_rounds" and isinstance(lane_statuses, list):
        if "progress" in lane_statuses:
            stop_reason = "progress"
    emit_frontier(context, frontier)
    return SchedulerRunResult(
        hypotheses=hypotheses,
        specialist_results=tuple(results),
        solved=stop_reason == "solved",
        stop_reason=stop_reason,
    )


def retire_terminal_lane(
    context: dict[str, object],
    frontier: AdaptiveFrontier,
    lane_id: FrontierLaneId,
    results: list[SpecialistResult],
    novel_evidence: bool = False,
) -> bool:
    statuses = context.get("_lane_slice_status_by_hypothesis")
    if not isinstance(statuses, dict) or any(item.flag_candidates for item in results):
        return False
    status = statuses.get(str(lane_id))
    if status == "stalled":
        reason = (
            "stalled:validated_evidence_exhausted"
            if novel_evidence
            else "stalled:no_novel_validated_evidence"
        )
    elif status == "failed":
        reason = "failed:no_successful_receipt"
    elif status == "solved":
        reason = "solved:no_verified_candidate"
    else:
        return False
    frontier.retire(lane_id, reason=reason)
    return True


def emit_frontier(
    context: dict[str, object], frontier: AdaptiveFrontier
) -> None:
    observer = context.get("event_observer")
    if not callable(observer):
        return
    for event in frontier.drain_events():
        observer(event.event_type, event.to_dict())


def aggregate_lane_results(
    lane_id: FrontierLaneId, results: list[SpecialistResult]
) -> SpecialistResult:
    return SpecialistResult(
        hypothesis_id=str(lane_id),
        status=(
            "confirmed" if any(item.status == "confirmed" for item in results) else "inconclusive"
        ),
        facts=[fact for item in results for fact in item.facts],
        artifacts=[artifact for item in results for artifact in item.artifacts],
    )
