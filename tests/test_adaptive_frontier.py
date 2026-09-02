from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from ctf_agent.budget_types import ProgressEvidence
from ctf_agent.frontier import AdaptiveFrontier, FrontierLaneId, FrontierLaneStatus
from ctf_agent.lanes import ProvenancedFact
from ctf_agent.models.base import ModelRequest, ModelResponse
from ctf_agent.scheduler import Scheduler, StaticHypothesisPlanner
from ctf_agent.schemas import FlagCandidate, Hypothesis, SpecialistResult
from ctf_agent.workers import CommandPolicy, LaneWorkspace, WorkerBudget, WorkerCore


def _verified_progress(results: tuple[SpecialistResult, ...]) -> ProgressEvidence:
    facts = tuple(
        ProvenancedFact(
            fact=fact,
            source="command",
            evidence_sha256=hashlib.sha256(fact.encode()).hexdigest(),
            status="validated",
            sequence=index,
        )
        for index, fact in enumerate(
            (fact for result in results for fact in result.facts), 1
        )
    )
    return ProgressEvidence(facts=facts)


def _hypothesis(index: int) -> Hypothesis:
    return Hypothesis(
        id=f"H{index}",
        claim=f"independent lead {index}",
        expected_signal="novel evidence",
        cost="low",
        confidence=0.5,
        kill_condition="stagnant",
        success_condition="verified candidate",
    )


class RecordingSpecialist:
    name = "recording"

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def supports(self, _claim: str) -> bool:
        return True

    async def solve(
        self, hypothesis: Hypothesis, _context: dict[str, object]
    ) -> SpecialistResult:
        self.calls.append(hypothesis.id)
        return SpecialistResult(
            hypothesis_id=hypothesis.id,
            status="inconclusive",
            next_action="continue",
        )


def test_six_hypotheses_receive_first_quantum_with_active_width_three() -> None:
    # Given: a pool larger than the active frontier.
    calls: list[str] = []
    scheduler = Scheduler(
        StaticHypothesisPlanner([_hypothesis(index) for index in range(1, 7)]),
        (RecordingSpecialist(calls),),
        max_concurrency=3,
        max_rounds=1,
    )

    # When: the scheduler admits one bounded quantum per hypothesis.
    result = asyncio.run(scheduler.run({}))

    # Then: all six pool entries run exactly once despite active width three.
    assert [item.id for item in result.hypotheses] == [f"H{i}" for i in range(1, 7)]
    assert calls == [f"H{i}" for i in range(1, 7)]
    assert len(result.specialist_results) == 6


def test_novel_evidence_deterministically_earns_extra_depth() -> None:
    # Given: one lane adds a novel fact while three peers stall.
    calls: list[str] = []

    class NovelSpecialist(RecordingSpecialist):
        async def solve(
            self, hypothesis: Hypothesis, _context: dict[str, object]
        ) -> SpecialistResult:
            self.calls.append(hypothesis.id)
            return SpecialistResult(
                hypothesis_id=hypothesis.id,
                status="inconclusive",
                facts=["new decoder invariant"] if hypothesis.id == "H2" else [],
            )

    scheduler = Scheduler(
        StaticHypothesisPlanner([_hypothesis(index) for index in range(1, 5)]),
        (NovelSpecialist(calls),),
        max_concurrency=1,
        max_rounds=2,
    )

    # When: one extra adaptive quantum is available after the fair first pass.
    asyncio.run(
        scheduler.run({"progress_evidence_provider": _verified_progress})
    )

    # Then: no lane deepens before all first quanta and the novel lane wins the tie.
    assert calls[:4] == ["H1", "H2", "H3", "H4"]
    assert calls[4] == "H2"


def test_frontier_quanta_resume_deterministically_from_persisted_events() -> None:
    # Given: a completed fair pass whose transition events survive process loss.
    calls: list[str] = []
    events: list[dict[str, str | int | bool | None]] = []

    class NovelSpecialist(RecordingSpecialist):
        async def solve(
            self, hypothesis: Hypothesis, _context: dict[str, object]
        ) -> SpecialistResult:
            self.calls.append(hypothesis.id)
            return SpecialistResult(
                hypothesis_id=hypothesis.id,
                status="inconclusive",
                facts=["novel invariant"] if hypothesis.id == "H2" else [],
            )

    def observe(_event_type: str, payload: dict[str, str | int | bool | None]) -> None:
        events.append(payload)

    hypotheses = [_hypothesis(index) for index in range(1, 5)]
    first = Scheduler(
        StaticHypothesisPlanner(hypotheses),
        (NovelSpecialist(calls),),
        max_concurrency=2,
        max_rounds=1,
    )
    asyncio.run(
        first.run(
            {
                "event_observer": observe,
                "progress_evidence_provider": _verified_progress,
            }
        )
    )
    assert calls == ["H1", "H2", "H3", "H4"]

    # When: a new scheduler restores only persisted machine events.
    resumed = Scheduler(
        StaticHypothesisPlanner(hypotheses),
        (NovelSpecialist(calls),),
        max_concurrency=2,
        max_rounds=2,
    )
    asyncio.run(
        resumed.run(
            {
                "event_observer": observe,
                "frontier_events": list(events),
                "progress_evidence_provider": _verified_progress,
            }
        )
    )

    # Then: no first quantum repeats and novelty deterministically receives depth first.
    assert calls[4] == "H2"


def test_frontier_semantic_dedupe_and_candidate_states_are_append_only() -> None:
    # Given: equivalent hypotheses and one independent replacement.
    duplicate = _hypothesis(1).model_copy(
        update={"id": "duplicate", "claim": "  INDEPENDENT   lead 1 "}
    )
    frontier = AdaptiveFrontier([_hypothesis(1), duplicate], active_width=2)

    # When: a lane is admitted, paused by a provisional rejection, then resumed and verified.
    lane_id = FrontierLaneId("H1")
    frontier.admit(lane_id)
    frontier.provisional(lane_id, "false-sha")
    frontier.reject(lane_id, "false-sha")
    frontier.admit(lane_id)
    frontier.provisional(lane_id, "accepted-sha")
    frontier.verify(lane_id, "accepted-sha")

    # Then: the pool deduplicates semantically and every machine state transition remains ordered.
    assert len(frontier.lanes) == 1
    assert frontier.lanes[0].status is FrontierLaneStatus.VERIFIED
    assert [event.sequence for event in frontier.events] == list(
        range(1, len(frontier.events) + 1)
    )
    assert [
        event.event_type
        for event in frontier.events
        if event.event_type.startswith("candidate.") or event.event_type == "lane.resumed"
    ] == [
        "candidate.provisional",
        "candidate.rejected",
        "lane.resumed",
        "candidate.provisional",
        "candidate.verified",
    ]


class CandidateSpecialist:
    def __init__(
        self,
        name: str,
        value: str,
        *,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.name = name
        self.value = value
        self.gate = gate

    def supports(self, _claim: str) -> bool:
        return True

    async def solve(
        self, hypothesis: Hypothesis, _context: dict[str, object]
    ) -> SpecialistResult:
        if self.gate is not None:
            await self.gate.wait()
        return SpecialistResult(
            hypothesis_id=hypothesis.id,
            status="confirmed",
            flag_candidates=[
                FlagCandidate(
                    value=self.value,
                    source_artifact="solve.py",
                    source_location="stdout",
                    solver_command="python solve.py",
                )
            ],
            next_action=self.name,
        )


def test_false_candidate_rejection_resumes_another_lane() -> None:
    # Given: a fast false candidate and a second lane paused on an exact signal.
    release_second = asyncio.Event()
    verification_order: list[str] = []

    async def verify(candidate: FlagCandidate) -> bool:
        verification_order.append(candidate.value)
        if candidate.value == "flag{false}":
            release_second.set()
            return False
        return True

    scheduler = Scheduler(
        StaticHypothesisPlanner([_hypothesis(1)]),
        (
            CandidateSpecialist("first", "flag{false}"),
            CandidateSpecialist("second", "flag{verified}", gate=release_second),
        ),
        max_concurrency=2,
    )

    # When: provisional candidates are checked by the controller-owned verifier.
    result = asyncio.run(scheduler.run({"candidate_verifier": verify}))

    # Then: rejection does not cancel the other lane and only verified success stops.
    assert verification_order == ["flag{false}", "flag{verified}"]
    assert result.solved is True
    assert result.accepted_flags == ("flag{verified}",)
    assert [item.next_action for item in result.specialist_results] == ["first", "second"]


class QueueBackend:
    def __init__(self, decisions: list[dict[str, Any]]) -> None:
        self.decisions = list(decisions)

    async def complete(self, _request: ModelRequest) -> ModelResponse:
        return ModelResponse(content=json.dumps(self.decisions.pop(0)))


def test_nonzero_and_repeated_output_do_not_count_as_progress(tmp_path: Path) -> None:
    # Given: a failed command followed by two generations producing identical output.
    workspace = LaneWorkspace(tmp_path / "lane")
    worker = WorkerCore(
        QueueBackend(
            [
                {"action": "run", "argv": [sys.executable, "-c", "raise SystemExit(2)"]},
                {
                    "action": "write_file",
                    "path": "solve.py",
                    "content": "print('same')\n",
                },
                {"action": "run", "argv": [sys.executable, "solve.py"]},
                {
                    "action": "write_file",
                    "path": "solve.py",
                    "content": "# changed\nprint('same')\n",
                },
                {"action": "run", "argv": [sys.executable, "solve.py"]},
                {"action": "finish", "message": "done"},
            ]
        ),
        workspace,
        budget=WorkerBudget(max_steps=6, max_commands=3, max_no_progress_steps=6),
        policy=CommandPolicy(
            allowed_argv0={Path(sys.executable).name}, local_test_mode=True
        ),
    )

    # When: the controller evaluates command outcomes and output identities.
    result = asyncio.run(worker.run("progress truth"))

    # Then: failure and repeated output are stagnant, while changed solve.py reruns.
    run_reports = [report for report in result.reports if report.action == "run"]
    assert [report.status for report in run_reports] == ["failed", "ok", "ok"]
    assert [report.exit_code for report in run_reports] == [2, 0, 0]
    assert [report.made_progress for report in run_reports] == [False, True, False]
    assert run_reports[1].command_fingerprint != run_reports[2].command_fingerprint
