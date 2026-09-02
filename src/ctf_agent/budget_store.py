"""Transactional SQLite persistence for model budget snapshots and leases."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from ctf_agent.budget_progress import BudgetProgressStore
from ctf_agent.budget_reader import ModelBudgetReader
from ctf_agent.budget_schema import initialize_budget_schema
from ctf_agent.budget_types import (
    BudgetExhaustedError,
    BudgetLease,
    BudgetLeaseId,
    BudgetLeaseStateError,
    BudgetNotFoundError,
    BudgetPersistenceError,
    BudgetPolicy,
    BudgetPurpose,
    BudgetRequest,
    BudgetRequestId,
    BudgetRole,
    BudgetSnapshot,
    LeaseStatus,
    ProgressEvidence,
)


@dataclass(frozen=True, slots=True)
class _AdmissionState:
    role_used: int
    consumed: int
    active_limit: int
    floor: int
    verifier_used: int
    retry_reserve: int
    verifier_candidate_limit: int


class ModelBudgetStore:
    """Own atomic budget state transitions in one SQLite database."""

    def __init__(self, database: Path, run_id: str) -> None:
        self.database = database
        self.run_id = run_id

    def create(self, policy: BudgetPolicy) -> None:
        now = datetime.now(UTC).isoformat()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            initialize_budget_schema(connection)
            connection.execute(
                "INSERT OR IGNORE INTO model_budget_snapshots("
                "run_id,initial_limit,active_limit,hard_limit,verifier_floor,"
                "planner_soft_limit,max_extensions,extension_size,retry_reserve,"
                "verifier_candidate_limit,extension_count,requested,final_stop_reason,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.run_id,
                    policy.initial_limit,
                    policy.initial_limit,
                    policy.hard_limit,
                    policy.verifier_floor,
                    policy.planner_soft_limit,
                    policy.max_extensions,
                    policy.extension_size,
                    policy.retry_reserve,
                    policy.verifier_candidate_limit,
                    0,
                    0,
                    "",
                    now,
                    now,
                ),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO model_budget_role_totals(run_id,role) VALUES(?,?)",
                ((self.run_id, role.value) for role in BudgetRole),
            )

    def acquire(self, request: BudgetRequest) -> BudgetLease | BudgetExhaustedError:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM model_budget_leases WHERE run_id=? AND request_id=?",
                (self.run_id, request.request_id),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._lease(existing)
            snapshot = self._snapshot_row(connection)
            connection.execute(
                "UPDATE model_budget_snapshots SET requested=requested+1,updated_at=? "
                "WHERE run_id=?",
                (datetime.now(UTC).isoformat(), self.run_id),
            )
            connection.execute(
                "INSERT INTO model_budget_role_totals(run_id,role,requested) "
                "VALUES(?,?,1) ON CONFLICT(run_id,role) DO UPDATE SET "
                "requested=requested+1",
                (self.run_id, request.role.value),
            )
            consumed = self._active_count(connection)
            verifier = self._active_count(connection, BudgetRole.VERIFIER)
            floor = int(snapshot["verifier_floor"])
            role_used = self._active_count(connection, request.role)
            reason = self._denial_reason(
                request,
                _AdmissionState(
                    role_used,
                    consumed,
                    int(snapshot["active_limit"]),
                    floor,
                    verifier,
                    int(snapshot["retry_reserve"]),
                    int(snapshot["verifier_candidate_limit"]),
                ),
            )
            if reason is not None:
                connection.commit()
                return BudgetExhaustedError(
                    request.role, request.purpose, request.request_id, reason
                )
            solver_soft = max(
                0,
                int(snapshot["active_limit"])
                - floor
                - int(snapshot["planner_soft_limit"]),
            )
            borrowed = request.role is BudgetRole.SOLVER and role_used >= solver_soft
            lease_id = BudgetLeaseId(
                sha256(f"{self.run_id}\0{request.request_id}".encode()).hexdigest()
            )
            now = datetime.now(UTC).isoformat()
            connection.execute(
                "INSERT INTO model_budget_leases VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    lease_id,
                    self.run_id,
                    request.role.value,
                    request.purpose.value,
                    request.request_id,
                    LeaseStatus.RESERVED.value,
                    borrowed,
                    now,
                    now,
                ),
            )
            connection.commit()
        return BudgetLease(
            lease_id,
            request.request_id,
            request.role,
            request.purpose,
            LeaseStatus.RESERVED,
            borrowed,
        )

    def transition(
        self, lease_id: BudgetLeaseId, expected: LeaseStatus, target: LeaseStatus
    ) -> BudgetLease:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM model_budget_leases WHERE run_id=? AND lease_id=?",
                (self.run_id, lease_id),
            ).fetchone()
            if row is None:
                raise BudgetNotFoundError(f"lease:{lease_id}")
            actual = LeaseStatus(str(row["status"]))
            if actual is not expected:
                raise BudgetLeaseStateError(lease_id, expected, actual)
            now = datetime.now(UTC).isoformat()
            connection.execute(
                "UPDATE model_budget_leases SET status=?,updated_at=? WHERE lease_id=?",
                (target.value, now, lease_id),
            )
            connection.commit()
            updated = dict(row) | {"status": target.value, "updated_at": now}
        return self._lease(updated)

    def extend(self, evidence: ProgressEvidence) -> int:
        return BudgetProgressStore(self.database, self.run_id).extend(evidence)

    def snapshot(self) -> BudgetSnapshot:
        return ModelBudgetReader(self.database, self.run_id).snapshot()

    def finish(self, stop_reason: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE model_budget_snapshots SET final_stop_reason=?,updated_at=? "
                "WHERE run_id=?",
                (stop_reason, datetime.now(UTC).isoformat(), self.run_id),
            )

    @staticmethod
    def _denial_reason(
        request: BudgetRequest, state: _AdmissionState
    ) -> str | None:
        if (
            request.role is BudgetRole.VERIFIER
            and request.purpose is BudgetPurpose.VERIFY
            and state.verifier_used >= state.verifier_candidate_limit
            and state.verifier_candidate_limit > 0
        ):
            return "verifier candidate limit reached"
        if request.role is BudgetRole.VERIFIER and request.purpose is BudgetPurpose.RETRY:
            if state.verifier_used - state.floor >= state.retry_reserve:
                return "retry reserve exhausted"
        if request.role is BudgetRole.VERIFIER and state.role_used >= (
            state.floor + state.retry_reserve
        ):
            return "verifier reserve exhausted"
        reserved_unused = max(0, state.floor - state.verifier_used)
        admission_limit = state.active_limit - (
            0 if request.role is BudgetRole.VERIFIER else reserved_unused + state.retry_reserve
        )
        return "admission limit reached" if state.consumed >= admission_limit else None

    def _active_count(
        self, connection: sqlite3.Connection, role: BudgetRole | None = None
    ) -> int:
        if role is None:
            row = connection.execute(
                "SELECT COUNT(*) FROM model_budget_leases WHERE run_id=? "
                "AND status IN ('reserved','started','committed','recovered')",
                (self.run_id,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT COUNT(*) FROM model_budget_leases WHERE run_id=? AND role=? "
                "AND status IN ('reserved','started','committed','recovered')",
                (self.run_id, role.value),
            ).fetchone()
        return int(row[0])

    def _snapshot_row(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM model_budget_snapshots WHERE run_id=?", (self.run_id,)
        ).fetchone()
        if row is None:
            raise BudgetNotFoundError(f"run:{self.run_id}")
        if not isinstance(row, sqlite3.Row):
            raise BudgetPersistenceError("snapshot query returned a non-row value")
        return row

    @staticmethod
    def _lease(row: sqlite3.Row | dict[str, str | int]) -> BudgetLease:
        return BudgetLease(
            BudgetLeaseId(str(row["lease_id"])),
            BudgetRequestId(str(row["request_id"])),
            BudgetRole(str(row["role"])),
            BudgetPurpose(str(row["purpose"])),
            LeaseStatus(str(row["status"])),
            bool(row["borrowed"]),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection
