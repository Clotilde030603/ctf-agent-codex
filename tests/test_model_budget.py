from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import anyio
import pytest

from ctf_agent.budget import ModelBudgetBroker
from ctf_agent.budget_types import (
    BudgetLease,
    BudgetPolicy,
    BudgetPurpose,
    BudgetRequest,
    BudgetRequestId,
    BudgetRole,
)
from ctf_agent.schemas import RunRecord
from ctf_agent.state import StateStore


def _broker(tmp_path: Path, **options: int) -> ModelBudgetBroker:
    store = StateStore(tmp_path / "state.db")
    run_id = "budget-run"
    store.create(
        RunRecord(
            run_id=run_id,
            challenge_url="https://ctf.test/c/budget",
            run_dir=tmp_path,
        )
    )
    return store.model_budget_broker(run_id, BudgetPolicy(**options))


def test_budget_public_create_initializes_fresh_database(tmp_path: Path) -> None:
    database = tmp_path / "fresh.db"

    broker = ModelBudgetBroker.create(
        database,
        "orchestrator-g006",
        BudgetPolicy(
            initial_limit=5,
            hard_limit=7,
            verifier_floor=1,
            planner_soft_limit=1,
            max_extensions=1,
            extension_size=2,
        ),
    )

    assert broker.snapshot().run_id == "orchestrator-g006"
    assert database.is_file()


def _acquire_started(
    broker: ModelBudgetBroker, role: str, purpose: str, request_id: str
) -> BudgetLease:
    request = BudgetRequest(
        BudgetRole(role),
        BudgetPurpose(purpose),
        BudgetRequestId(request_id),
    )
    lease = anyio.run(broker.acquire, request)
    anyio.run(broker.start, lease.lease_id)
    return lease


def test_budget_candidate_on_last_admitted_solver_keeps_verifier_lease(
    tmp_path: Path,
) -> None:
    broker = _broker(tmp_path, initial_limit=3, hard_limit=3, verifier_floor=1)

    _acquire_started(broker, "planner", "plan", "plan-1")
    _acquire_started(broker, "solver", "solve", "solve-last")
    verifier = _acquire_started(broker, "verifier", "verify", "verify-candidate")

    assert verifier.role == "verifier"
    assert broker.snapshot().used == 3


def test_budget_no_candidate_verifier_reserve_is_reported_unused(tmp_path: Path) -> None:
    broker = _broker(tmp_path, initial_limit=4, hard_limit=4, verifier_floor=1)

    _acquire_started(broker, "solver", "solve", "solve-1")

    report = broker.snapshot()
    assert report.reserved == 1
    assert report.reserved_unused == 1


def test_budget_planner_unused_pool_can_be_borrowed(tmp_path: Path) -> None:
    broker = _broker(
        tmp_path,
        initial_limit=4,
        hard_limit=4,
        verifier_floor=1,
        planner_soft_limit=1,
    )

    for index in range(3):
        _acquire_started(broker, "solver", "solve", f"solve-{index}")

    assert broker.snapshot().borrowed == 1


def test_budget_three_concurrent_solvers_cannot_breach_verifier_floor(
    tmp_path: Path,
) -> None:
    broker = _broker(tmp_path, initial_limit=3, hard_limit=3, verifier_floor=1)
    barrier = Barrier(3)

    def admit(index: int) -> bool:
        barrier.wait()
        try:
            _acquire_started(broker, "solver", "solve", f"solve-{index}")
        except RuntimeError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=3) as pool:
        admitted = list(pool.map(admit, range(3)))

    assert admitted.count(True) == 2
    assert broker.snapshot().reserved_unused == 1


def test_budget_restart_recovers_ambiguous_started_request(tmp_path: Path) -> None:
    broker = _broker(tmp_path, initial_limit=4, hard_limit=4, verifier_floor=1)
    lease = _acquire_started(broker, "solver", "solve", "crashed-request")

    reopened = type(broker).open(broker.database, broker.run_id)
    snapshot = reopened.snapshot()

    assert snapshot.used == 1
    recovered = next(item for item in snapshot.leases if item.lease_id == lease.lease_id)
    assert recovered.status == "recovered"


def test_budget_retry_reserve_bounds_reviewer_retries(tmp_path: Path) -> None:
    broker = _broker(tmp_path, initial_limit=5, hard_limit=5, verifier_floor=2)

    _acquire_started(broker, "verifier", "verify", "review-1")
    _acquire_started(broker, "verifier", "retry", "review-2")

    with pytest.raises(RuntimeError, match="budget"):
        anyio.run(
            broker.acquire,
            BudgetRequest(
                BudgetRole.VERIFIER,
                BudgetPurpose.RETRY,
                BudgetRequestId("review-3"),
            ),
        )


def test_budget_release_restores_atomic_admission(tmp_path: Path) -> None:
    broker = _broker(tmp_path, initial_limit=2, hard_limit=2, verifier_floor=1)
    lease = anyio.run(
        broker.acquire,
        BudgetRequest(
            BudgetRole.SOLVER,
            BudgetPurpose.SOLVE,
            BudgetRequestId("not-started"),
        ),
    )

    anyio.run(broker.release, lease.lease_id)
    replacement = _acquire_started(broker, "solver", "solve", "replacement")

    assert replacement.status == "reserved"
    assert broker.snapshot().used == 1


def test_budget_rejects_arbitrary_extension_evidence(tmp_path: Path) -> None:
    # Given: an elastic broker with extension capacity.
    broker = _broker(
        tmp_path,
        initial_limit=2,
        hard_limit=3,
        verifier_floor=1,
        max_extensions=1,
    )

    # When/Then: an untyped caller cannot manufacture progress with prose.
    with pytest.raises(TypeError):
        method_name = "extend"
        getattr(broker, method_name)("arbitrary progress claim")


def test_budget_retry_and_candidate_reserves_are_independently_enforced(
    tmp_path: Path,
) -> None:
    # Given: one candidate admission and one retry are explicitly protected.
    broker = _broker(
        tmp_path,
        initial_limit=4,
        hard_limit=5,
        verifier_floor=1,
        retry_reserve=1,
        verifier_candidate_limit=1,
    )
    _acquire_started(broker, "solver", "solve", "solver-1")
    _acquire_started(broker, "solver", "solve", "solver-2")

    # When: another solver and a second candidate try to consume protected capacity.
    with pytest.raises(RuntimeError, match="budget"):
        _acquire_started(broker, "solver", "solve", "solver-blocked")
    _acquire_started(broker, "verifier", "verify", "candidate-1")
    with pytest.raises(RuntimeError, match="candidate"):
        _acquire_started(broker, "verifier", "verify", "candidate-2")

    # Then: the retry reserve remains available only to a retry lease.
    retry = _acquire_started(broker, "verifier", "retry", "retry-1")
    assert retry.purpose is BudgetPurpose.RETRY
    assert broker.snapshot().reserved_unused == 0


def test_budget_static_backend_avoids_unnecessary_verifier_reserve(tmp_path: Path) -> None:
    broker = _broker(tmp_path, initial_limit=2, hard_limit=2, verifier_floor=0)

    _acquire_started(broker, "solver", "solve", "solve-1")
    _acquire_started(broker, "solver", "solve", "solve-2")

    assert broker.snapshot().reserved == 0
    assert broker.snapshot().used == 2
