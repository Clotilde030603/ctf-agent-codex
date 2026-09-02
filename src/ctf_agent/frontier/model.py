"""Typed state and append-only events for the adaptive hypothesis frontier."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import NewType

from ctf_agent.schemas import Hypothesis

FrontierLaneId = NewType("FrontierLaneId", str)


class FrontierLaneStatus(StrEnum):
    QUEUED = "queued"
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"
    PROVISIONAL = "provisional"
    VERIFIED = "verified"


@dataclass(frozen=True, slots=True)
class FrontierScore:
    novelty: int = 0
    evidence: int = 0
    stagnation: int = 0


@dataclass(frozen=True, slots=True)
class FrontierLane:
    lane_id: FrontierLaneId
    hypothesis: Hypothesis
    ordinal: int
    status: FrontierLaneStatus = FrontierLaneStatus.QUEUED
    quanta: int = 0
    score: FrontierScore = FrontierScore()
    fact_fingerprints: frozenset[str] = frozenset()
    artifact_fingerprints: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class FrontierEvent:
    sequence: int
    event_type: str
    lane_id: FrontierLaneId | None
    state: FrontierLaneStatus | None
    values: tuple[tuple[str, str | int | bool], ...] = ()

    def to_dict(self) -> dict[str, str | int | bool | None]:
        payload: dict[str, str | int | bool | None] = {
            "sequence": self.sequence,
            "type": self.event_type,
            "lane_id": self.lane_id,
            "state": self.state.value if self.state is not None else None,
        }
        payload.update(dict(self.values))
        return payload


def semantic_hypothesis_key(hypothesis: Hypothesis) -> tuple[str, tuple[str, ...]]:
    claim = re.sub(r"\s+", " ", hypothesis.claim.strip().casefold())
    tools = tuple(sorted(tool.strip().casefold() for tool in hypothesis.required_tools))
    return claim, tools
