"""Deterministic admission, scoring, and candidate lifecycle controller."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Final

from ctf_agent.budget_progress import verified_evidence_identities
from ctf_agent.budget_types import ProgressEvidence
from ctf_agent.frontier.model import (
    FrontierEvent,
    FrontierLane,
    FrontierLaneId,
    FrontierLaneStatus,
    FrontierScore,
    semantic_hypothesis_key,
)
from ctf_agent.schemas import Hypothesis

MAX_ACTIVE_WIDTH: Final = 3


class AdaptiveFrontier:
    """Own mutable frontier scheduling state while exposing immutable lane snapshots."""

    def __init__(
        self,
        hypotheses: Iterable[Hypothesis],
        *,
        active_width: int = MAX_ACTIVE_WIDTH,
        minimum_quantum: int = 1,
        total_pool_cap: int | None = None,
    ) -> None:
        if not 1 <= active_width <= MAX_ACTIVE_WIDTH:
            msg = f"active_width must be between 1 and {MAX_ACTIVE_WIDTH}"
            raise ValueError(msg)
        if minimum_quantum < 1:
            msg = "minimum_quantum must be positive"
            raise ValueError(msg)
        initial_hypotheses = tuple(hypotheses)
        self.active_width = active_width
        self.minimum_quantum = minimum_quantum
        self.total_pool_cap = total_pool_cap or max(1, len(initial_hypotheses))
        self._lanes: dict[FrontierLaneId, FrontierLane] = {}
        self._semantic_keys: dict[tuple[str, tuple[str, ...]], FrontierLaneId] = {}
        self._events: list[FrontierEvent] = []
        self._max_active_observed = 0
        self._evidence_fingerprints: set[str] = set()
        for hypothesis in initial_hypotheses:
            self.add(hypothesis)
        self._append("frontier.created", values=(("total_pool", len(self._lanes)),))

    @property
    def lanes(self) -> tuple[FrontierLane, ...]:
        return tuple(sorted(self._lanes.values(), key=lambda lane: lane.ordinal))

    @property
    def events(self) -> tuple[FrontierEvent, ...]:
        return tuple(self._events)

    @property
    def max_active_observed(self) -> int:
        return self._max_active_observed

    def lane(self, lane_id: FrontierLaneId) -> FrontierLane | None:
        return self._lanes.get(lane_id)

    def restore_lane(self, lane: FrontierLane) -> None:
        if lane.lane_id in self._lanes:
            self._lanes[lane.lane_id] = lane

    def drain_events(self) -> tuple[FrontierEvent, ...]:
        events = tuple(self._events)
        self._events.clear()
        return events

    def add(self, hypothesis: Hypothesis) -> FrontierLaneId | None:
        key = semantic_hypothesis_key(hypothesis)
        if key in self._semantic_keys:
            existing = self._semantic_keys[key]
            self._append(
                "lane.deduplicated",
                lane_id=existing,
                values=(("hypothesis_id", hypothesis.id),),
            )
            return None
        if self._live_pool_size() >= self.total_pool_cap:
            self._append(
                "lane.pool_full",
                values=(("hypothesis_id", hypothesis.id), ("total_pool", self._live_pool_size())),
            )
            return None
        lane_id = FrontierLaneId(hypothesis.id)
        suffix = 2
        while lane_id in self._lanes:
            lane_id = FrontierLaneId(f"{hypothesis.id}-{suffix}")
            suffix += 1
        lane = FrontierLane(lane_id, hypothesis, len(self._lanes))
        self._lanes[lane_id] = lane
        self._semantic_keys[key] = lane_id
        self._append("lane.queued", lane_id=lane_id, state=lane.status)
        return lane_id

    def replace(self, lane_id: FrontierLaneId, hypothesis: Hypothesis) -> FrontierLaneId | None:
        if self._lanes[lane_id].status is not FrontierLaneStatus.RETIRED:
            self.retire(lane_id, reason="replaced:controller_replan")
        replacement = self.add(hypothesis)
        if replacement is not None:
            self._append(
                "lane.replaced",
                lane_id=replacement,
                state=FrontierLaneStatus.QUEUED,
                values=(
                    ("replaced_lane", lane_id),
                    ("total_pool", self._live_pool_size()),
                ),
            )
        return replacement

    def admit(self, lane_id: FrontierLaneId) -> FrontierLane:
        lane = self._lanes[lane_id]
        active = sum(item.status is FrontierLaneStatus.ACTIVE for item in self._lanes.values())
        if active >= self.active_width:
            msg = "active frontier width exhausted"
            raise RuntimeError(msg)
        activated = replace(lane, status=FrontierLaneStatus.ACTIVE)
        self._lanes[lane_id] = activated
        self._max_active_observed = max(self._max_active_observed, active + 1)
        event_type = "lane.resumed" if lane.status is FrontierLaneStatus.PAUSED else "lane.admitted"
        self._append(event_type, lane_id=lane_id, state=activated.status)
        return activated

    def next_lane_ids(self, limit: int | None = None) -> tuple[FrontierLaneId, ...]:
        available = [
            lane
            for lane in self._lanes.values()
            if lane.status in {FrontierLaneStatus.QUEUED, FrontierLaneStatus.PAUSED}
        ]
        available.sort(key=self._priority)
        capacity = self.active_width if limit is None else min(limit, self.active_width)
        return tuple(lane.lane_id for lane in available[:capacity])

    def observe(self, lane_id: FrontierLaneId, progress: ProgressEvidence) -> FrontierLane:
        lane = self._lanes[lane_id]
        identities = frozenset(verified_evidence_identities(progress))
        novel_identities = identities - self._evidence_fingerprints
        novel_facts = frozenset(
            identity for identity in novel_identities if identity.startswith("fact:")
        )
        novel_artifacts = frozenset(
            identity for identity in novel_identities if identity.startswith("artifact:")
        )
        novelty = len(novel_identities)
        evidence = len(novel_facts) * 2 + novelty - len(novel_facts)
        penalty = int(novelty == 0)
        score = FrontierScore(
            novelty=lane.score.novelty + novelty,
            evidence=lane.score.evidence + evidence,
            stagnation=0 if novelty else lane.score.stagnation + penalty,
        )
        terminal_status = (
            lane.status
            if lane.status
            in {FrontierLaneStatus.PROVISIONAL, FrontierLaneStatus.VERIFIED}
            else FrontierLaneStatus.PAUSED
        )
        updated = replace(
            lane,
            status=terminal_status,
            quanta=lane.quanta + 1,
            score=score,
            fact_fingerprints=lane.fact_fingerprints | novel_facts,
            artifact_fingerprints=lane.artifact_fingerprints | novel_artifacts,
        )
        self._lanes[lane_id] = updated
        self._evidence_fingerprints.update(novel_identities)
        self._append(
            "lane.quantum_completed",
            lane_id=lane_id,
            state=updated.status,
            values=(
                ("quantum", updated.quanta),
                ("novelty", novelty),
                ("evidence", evidence),
                ("penalty", penalty),
                ("evidence_identities", "|".join(sorted(novel_identities))),
                ("stagnation", updated.score.stagnation),
                ("total_novelty", updated.score.novelty),
                ("total_evidence", updated.score.evidence),
            ),
        )
        return updated

    def provisional(self, lane_id: FrontierLaneId, candidate_sha256: str) -> None:
        lane = replace(self._lanes[lane_id], status=FrontierLaneStatus.PROVISIONAL)
        self._lanes[lane_id] = lane
        self._append(
            "candidate.provisional",
            lane_id=lane_id,
            state=lane.status,
            values=(("candidate_sha256", candidate_sha256),),
        )

    def reject(self, lane_id: FrontierLaneId, candidate_sha256: str) -> None:
        self._append(
            "candidate.rejected",
            lane_id=lane_id,
            state=FrontierLaneStatus.PROVISIONAL,
            values=(("candidate_sha256", candidate_sha256),),
        )
        self._lanes[lane_id] = replace(
            self._lanes[lane_id], status=FrontierLaneStatus.PAUSED
        )

    def verify(self, lane_id: FrontierLaneId, candidate_sha256: str) -> None:
        lane = replace(self._lanes[lane_id], status=FrontierLaneStatus.VERIFIED)
        self._lanes[lane_id] = lane
        self._append(
            "candidate.verified",
            lane_id=lane_id,
            state=lane.status,
            values=(("candidate_sha256", candidate_sha256),),
        )

    def restore_evidence_identities(self, identities: Iterable[str]) -> None:
        self._evidence_fingerprints.update(identities)

    def retire(self, lane_id: FrontierLaneId, *, reason: str) -> None:
        lane = replace(self._lanes[lane_id], status=FrontierLaneStatus.RETIRED)
        self._lanes[lane_id] = lane
        self._append(
            "lane.retired",
            lane_id=lane_id,
            state=lane.status,
            values=(("reason", reason),),
        )

    def pause_active(self) -> None:
        for lane in self.lanes:
            if lane.status is FrontierLaneStatus.ACTIVE:
                paused = replace(lane, status=FrontierLaneStatus.PAUSED)
                self._lanes[lane.lane_id] = paused
                self._append("lane.paused", lane_id=lane.lane_id, state=paused.status)

    def _live_pool_size(self) -> int:
        return sum(
            lane.status is not FrontierLaneStatus.RETIRED for lane in self._lanes.values()
        )

    def _priority(self, lane: FrontierLane) -> tuple[int, int, int, int, int]:
        needs_quantum = 0 if lane.quanta < self.minimum_quantum else 1
        return (
            needs_quantum,
            -lane.score.novelty,
            -lane.score.evidence,
            lane.score.stagnation,
            lane.ordinal,
        )

    def _append(
        self,
        event_type: str,
        *,
        lane_id: FrontierLaneId | None = None,
        state: FrontierLaneStatus | None = None,
        values: tuple[tuple[str, str | int | bool], ...] = (),
    ) -> None:
        self._events.append(
            FrontierEvent(len(self._events) + 1, event_type, lane_id, state, values)
        )
