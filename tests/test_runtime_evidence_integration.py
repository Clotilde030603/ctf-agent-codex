from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from ctf_agent.budget_types import (
    BudgetExhaustedError,
    BudgetPolicy,
    BudgetPurpose,
    BudgetRequest,
    BudgetRequestId,
    BudgetRole,
    ProgressEvidence,
)
from ctf_agent.config import Settings
from ctf_agent.lanes import (
    LaneCheckpoint,
    LaneModelIdentity,
    LaneRunResult,
    LaneStatus,
    ProvenancedFact,
)
from ctf_agent.models.claude import ClaudeStubBackend
from ctf_agent.scheduler import Scheduler
from ctf_agent.schemas import Hypothesis, RunRecord, SpecialistResult
from ctf_agent.specialists.model import ModelSolverSpecialist
from ctf_agent.specialists.model_lane import prepare_lane
from ctf_agent.state import StateStore


def _hypothesis(identifier: str) -> Hypothesis:
    return Hypothesis(
        id=identifier,
        claim=f"lead {identifier}",
        expected_signal="validated evidence",
        cost="low",
        confidence=0.5,
        kill_condition="no validated evidence",
        success_condition="verified candidate",
    )


class RepeatingSpecialist:
    name = "repeating"

    def supports(self, _claim: str) -> bool:
        return True

    async def solve(
        self, hypothesis: Hypothesis, _context: dict[str, object]
    ) -> SpecialistResult:
        return SpecialistResult(
            hypothesis_id=hypothesis.id,
            status="inconclusive",
            facts=["model says this is progress"],
            artifacts=["model-controlled/path.txt"],
        )


def _validated_fact(value: str) -> ProvenancedFact:
    return ProvenancedFact(
        fact=value,
        source="command",
        evidence_sha256=hashlib.sha256(value.encode()).hexdigest(),
        status="validated",
        sequence=1,
    )


def test_scheduler_frontier_ignores_raw_model_progress_and_penalizes_repetition() -> None:
    events: list[tuple[str, dict[str, str | int | bool | None]]] = []
    scheduler = Scheduler(
        planner=_SequencePlanner(((_hypothesis("H1"),),)),
        specialists=(RepeatingSpecialist(),),
        max_concurrency=1,
        max_rounds=2,
    )

    result = asyncio.run(
        scheduler.run(
            {
                "event_observer": lambda event_type, payload: events.append(
                    (event_type, payload)
                ),
                "progress_evidence_provider": lambda _results: ProgressEvidence(),
            }
        )
    )

    completed = [
        payload
        for event_type, payload in events
        if event_type == "lane.quantum_completed"
    ]
    assert result.stop_reason == "no_progress"
    assert completed
    assert all(payload["novelty"] == 0 for payload in completed)
    assert all(payload["penalty"] == 1 for payload in completed)


def test_scheduler_invokes_extension_only_once_for_replayed_validated_evidence() -> None:
    calls: list[ProgressEvidence] = []
    evidence = ProgressEvidence(facts=(_validated_fact("controller proof"),))
    scheduler = Scheduler(
        planner=_SequencePlanner(((_hypothesis("H1"),),)),
        specialists=(RepeatingSpecialist(),),
        max_concurrency=1,
        max_rounds=2,
    )

    asyncio.run(
        scheduler.run(
            {
                "progress_evidence_provider": lambda _results: evidence,
                "budget_extension_decider": lambda observed: calls.append(observed) or 1,
            }
        )
    )

    assert calls == [evidence]


class _SequencePlanner:
    def __init__(self, plans: tuple[tuple[Hypothesis, ...], ...]) -> None:
        self.plans = plans
        self.calls = 0

    async def plan(self, _context: dict[str, object]) -> tuple[Hypothesis, ...]:
        index = min(self.calls, len(self.plans) - 1)
        self.calls += 1
        return self.plans[index]


class StalledSliceSpecialist:
    name = "stalled-slice"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def supports(self, _claim: str) -> bool:
        return True

    async def run_slice(
        self,
        hypothesis: Hypothesis,
        _context: dict[str, object],
        *,
        max_steps: int | None = None,
    ) -> LaneRunResult:
        self.calls.append(hypothesis.id)
        checkpoint = LaneCheckpoint(
            lane_id=hypothesis.id,
            run_id="run-replacement",
            hypothesis_id=hypothesis.id,
            hypothesis_revision="r1",
            category="misc",
            model_identity=LaneModelIdentity(
                specialist=self.name,
                model="test",
                effort="high",
                skill_sha256="skill",
                capability_sha256="capability",
                attachment_sha256="attachment",
            ),
            status=LaneStatus.STALLED,
            hypothesis=hypothesis.claim,
            restatement=hypothesis.claim,
        )
        result = SpecialistResult(hypothesis_id=hypothesis.id, status="inconclusive")
        return LaneRunResult(
            status=LaneStatus.STALLED,
            checkpoint=checkpoint,
            specialist_result=result,
        )


def test_scheduler_replaces_retired_lane_with_evidence_backed_reason_under_pool_cap() -> None:
    planner = _SequencePlanner(((_hypothesis("H1"),), (_hypothesis("H2"),)))
    specialist = StalledSliceSpecialist()
    events: list[tuple[str, dict[str, str | int | bool | None]]] = []
    scheduler = Scheduler(
        planner=planner,
        specialists=(specialist,),
        max_concurrency=1,
        max_rounds=1,
    )

    asyncio.run(
        scheduler.run(
            {
                "event_observer": lambda event_type, payload: events.append(
                    (event_type, payload)
                ),
                "progress_evidence_provider": lambda _results: ProgressEvidence(),
            }
        )
    )

    retired = [payload for event_type, payload in events if event_type == "lane.retired"]
    replaced = [payload for event_type, payload in events if event_type == "lane.replaced"]
    assert planner.calls == 2
    assert specialist.calls == ["H1", "H2"]
    assert retired[0]["reason"] == "stalled:no_novel_validated_evidence"
    assert replaced[0]["replaced_lane"] == "H1"
    assert replaced[0]["total_pool"] == 1


def test_production_lane_store_emits_lifecycle_to_run_observer(tmp_path: Path) -> None:
    observed: list[str] = []
    run_dir = tmp_path / "run"
    (run_dir / "files").mkdir(parents=True)
    specialist = ModelSolverSpecialist(
        Settings(runs_dir=tmp_path / "runs"),
        backend_factory=lambda _settings, _role, _workspace: ClaudeStubBackend([]),
        local_test_mode=True,
    )

    prepare_lane(
        specialist,
        _hypothesis("H1"),
        {
            "run_dir": str(run_dir),
            "run_id": "run-observer",
            "state_database": str(run_dir / "state.db"),
            "event_observer": lambda event_type, _payload: observed.append(event_type),
        },
    )

    assert observed == ["lane.checkpoint.created"]


def test_budget_report_persists_per_role_totals_and_final_stop_reason(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create(
        RunRecord(
            run_id="run-budget-report",
            challenge_url="https://ctf.test/challenges/report",
            run_dir=tmp_path,
        )
    )
    broker = store.model_budget_broker(
        "run-budget-report",
        BudgetPolicy(
            initial_limit=2,
            hard_limit=3,
            verifier_floor=1,
            max_extensions=1,
        ),
    )
    request = BudgetRequest(
        BudgetRole.SOLVER,
        BudgetPurpose.SOLVE,
        BudgetRequestId("solver-request"),
    )
    lease = asyncio.run(broker.acquire(request))
    asyncio.run(broker.start(lease.lease_id))
    asyncio.run(broker.commit(lease.lease_id))
    with pytest.raises(BudgetExhaustedError):
        asyncio.run(
            broker.acquire(
                BudgetRequest(
                    BudgetRole.SOLVER,
                    BudgetPurpose.SOLVE,
                    BudgetRequestId("denied-solver-request"),
                )
            )
        )
    broker.extend(ProgressEvidence(facts=(_validated_fact("extension proof"),)))

    broker.finish("no_progress")
    report = type(broker).open(broker.database, broker.run_id, recover=False).snapshot().to_dict()

    assert report["final_stop_reason"] == "no_progress"
    assert report["roles"]["solver"] == {
        "requested": 2,
        "used": 1,
        "reserved": 0,
        "borrowed": 1,
        "extended": 1,
    }
    assert set(report["roles"]) == {"planner", "solver", "verifier"}
