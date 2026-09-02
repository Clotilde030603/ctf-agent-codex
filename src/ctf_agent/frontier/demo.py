"""Dependency-free deterministic frontier demonstration."""

from __future__ import annotations

import hashlib
from enum import StrEnum

from ctf_agent.budget_types import ProgressEvidence
from ctf_agent.frontier.controller import AdaptiveFrontier
from ctf_agent.frontier.model import FrontierLaneId
from ctf_agent.lanes import ProvenancedFact
from ctf_agent.schemas import Hypothesis


class DemoScenario(StrEnum):
    FALSE_CANDIDATE = "false-candidate"


def false_candidate_demo() -> dict[
    str,
    str | int | list[str] | list[dict[str, str | int | bool | None]] | dict[str, int],
]:
    hypotheses = tuple(_hypothesis(index) for index in range(1, 7))
    frontier = AdaptiveFrontier(hypotheses, active_width=3)
    first_wave = frontier.next_lane_ids()
    for lane_id in first_wave:
        frontier.admit(lane_id)
    for lane_id in first_wave:
        fact = "novel decoder fact"
        frontier.observe(
            lane_id,
            ProgressEvidence(
                facts=(
                    ProvenancedFact(
                        fact=fact,
                        source="command",
                        evidence_sha256=hashlib.sha256(fact.encode()).hexdigest(),
                        status="validated",
                        sequence=1,
                    ),
                )
                if lane_id == FrontierLaneId("H2")
                else ()
            ),
        )
    for lane_id in frontier.next_lane_ids():
        frontier.admit(lane_id)
        frontier.observe(lane_id, ProgressEvidence())

    first = FrontierLaneId("H2")
    frontier.admit(first)
    false_hash = hashlib.sha256(b"flag{false}").hexdigest()
    frontier.provisional(first, false_hash)
    frontier.reject(first, false_hash)
    resumed = FrontierLaneId("H1")
    frontier.admit(resumed)
    accepted_hash = hashlib.sha256(b"flag{verified}").hexdigest()
    frontier.provisional(resumed, accepted_hash)
    frontier.verify(resumed, accepted_hash)
    return {
        "scenario": "false-candidate",
        "active_width": frontier.active_width,
        "max_active_observed": frontier.max_active_observed,
        "total_pool": len(frontier.lanes),
        "budget": {"used": 8, "limit": 9, "verifier_floor": 1},
        "rejected_candidates": [false_hash],
        "accepted_candidates": [accepted_hash],
        "events": [event.to_dict() for event in frontier.events],
    }


def _hypothesis(index: int) -> Hypothesis:
    return Hypothesis(
        id=f"H{index}",
        claim=f"independent demo lead {index}",
        expected_signal="novel evidence",
        cost="low",
        confidence=0.5,
        kill_condition="stagnant",
        success_condition="verified candidate",
    )
