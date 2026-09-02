"""Crash and legacy recovery for persistent model budget leases."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, assert_never

from ctf_agent.budget_types import (
    BudgetLeaseId,
    BudgetPurpose,
    BudgetRequest,
    BudgetRequestId,
    BudgetRole,
    LeaseStatus,
)


class ModelBudgetRecovery:
    def __init__(self, database: Path, run_id: str) -> None:
        self.database = database
        self.run_id = run_id

    def recover_started(self) -> int:
        """Consume ambiguous started calls and free calls proven not to have started."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = datetime.now(UTC).isoformat()
            cursor = connection.execute(
                "UPDATE model_budget_leases SET status=?,updated_at=? "
                "WHERE run_id=? AND status=?",
                (
                    LeaseStatus.RECOVERED.value,
                    now,
                    self.run_id,
                    LeaseStatus.STARTED.value,
                ),
            )
            connection.execute(
                "UPDATE model_budget_leases SET status=?,updated_at=? "
                "WHERE run_id=? AND status=?",
                (
                    LeaseStatus.RELEASED.value,
                    now,
                    self.run_id,
                    LeaseStatus.RESERVED.value,
                ),
            )
            connection.commit()
        return cursor.rowcount

    def reconcile_events(self, events: Sequence[Mapping[str, Any]]) -> int:
        requests: list[BudgetRequest] = []
        for event in events:
            if event.get("event_type") != "model.request":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            try:
                role = BudgetRole(str(payload.get("role", BudgetRole.SOLVER.value)))
            except ValueError:
                role = BudgetRole.SOLVER
            try:
                purpose = BudgetPurpose(str(payload.get("purpose", "")))
            except ValueError:
                match role:
                    case BudgetRole.PLANNER:
                        purpose = BudgetPurpose.PLAN
                    case BudgetRole.SOLVER:
                        purpose = BudgetPurpose.SOLVE
                    case BudgetRole.VERIFIER:
                        purpose = BudgetPurpose.VERIFY
                    case unreachable:
                        assert_never(unreachable)
            event_id = str(event.get("id", len(requests) + 1))
            request_id = BudgetRequestId(
                str(payload.get("request_id") or f"legacy-event:{event_id}")
            )
            requests.append(BudgetRequest(role, purpose, request_id))
        return self.reconcile_legacy(tuple(requests))

    def reconcile_legacy(self, requests: tuple[BudgetRequest, ...]) -> int:
        """Conservatively consume model.request events predating persistent leases."""
        recovered = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for request in requests:
                exists = connection.execute(
                    "SELECT 1 FROM model_budget_leases WHERE run_id=? AND request_id=?",
                    (self.run_id, request.request_id),
                ).fetchone()
                if exists is not None:
                    continue
                now = datetime.now(UTC).isoformat()
                lease_id = BudgetLeaseId(
                    sha256(f"{self.run_id}\0{request.request_id}".encode()).hexdigest()
                )
                connection.execute(
                    "INSERT INTO model_budget_leases VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        lease_id,
                        self.run_id,
                        request.role.value,
                        request.purpose.value,
                        request.request_id,
                        LeaseStatus.RECOVERED.value,
                        False,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE model_budget_snapshots SET requested=requested+1,updated_at=? "
                    "WHERE run_id=?",
                    (now, self.run_id),
                )
                recovered += 1
            connection.commit()
        return recovered

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection
