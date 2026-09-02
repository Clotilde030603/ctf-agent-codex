"""Deterministic adaptive-frontier reconstruction from durable events."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import assert_never

from ctf_agent.frontier.controller import MAX_ACTIVE_WIDTH, AdaptiveFrontier
from ctf_agent.frontier.model import (
    FrontierLaneId,
    FrontierLaneStatus,
    FrontierScore,
)
from ctf_agent.schemas import Hypothesis


def restore_frontier(
    hypotheses: Iterable[Hypothesis],
    events: Iterable[Mapping[str, str | int | bool | None]],
    *,
    active_width: int = MAX_ACTIVE_WIDTH,
    minimum_quantum: int = 1,
) -> AdaptiveFrontier:
    frontier = AdaptiveFrontier(
        hypotheses,
        active_width=active_width,
        minimum_quantum=minimum_quantum,
    )
    frontier.drain_events()
    for event in events:
        raw_lane_id = event.get("lane_id")
        if not isinstance(raw_lane_id, str):
            continue
        lane = frontier.lane(FrontierLaneId(raw_lane_id))
        if lane is None:
            continue
        raw_state = event.get("state")
        if event.get("type") == "lane.quantum_completed":
            raw_identities = event.get("evidence_identities")
            if isinstance(raw_identities, str) and raw_identities:
                frontier.restore_evidence_identities(raw_identities.split("|"))
            frontier.restore_lane(
                replace(
                    lane,
                    status=FrontierLaneStatus.PAUSED,
                    quanta=_integer(event, "quantum", lane.quanta),
                    score=FrontierScore(
                        novelty=_integer(event, "total_novelty", lane.score.novelty),
                        evidence=_integer(event, "total_evidence", lane.score.evidence),
                        stagnation=_integer(event, "stagnation", lane.score.stagnation),
                    ),
                )
            )
        elif isinstance(raw_state, str):
            status = FrontierLaneStatus(raw_state)
            match status:
                case FrontierLaneStatus.ACTIVE:
                    restored_status = FrontierLaneStatus.PAUSED
                case (
                    FrontierLaneStatus.QUEUED
                    | FrontierLaneStatus.PAUSED
                    | FrontierLaneStatus.RETIRED
                    | FrontierLaneStatus.PROVISIONAL
                    | FrontierLaneStatus.VERIFIED
                ):
                    restored_status = status
                case unreachable:
                    assert_never(unreachable)
            frontier.restore_lane(replace(lane, status=restored_status))
    return frontier


def _integer(
    event: Mapping[str, str | int | bool | None], key: str, default: int
) -> int:
    value = event.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else default
