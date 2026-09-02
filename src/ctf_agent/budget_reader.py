"""Consistent read models for persisted model budget state."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ctf_agent.budget_types import (
    BudgetLease,
    BudgetLeaseId,
    BudgetNotFoundError,
    BudgetPurpose,
    BudgetRequestId,
    BudgetRole,
    BudgetRoleTotals,
    BudgetSnapshot,
    LeaseStatus,
)


class ModelBudgetReader:
    def __init__(self, database: Path, run_id: str) -> None:
        self.database = database
        self.run_id = run_id

    def snapshot(self) -> BudgetSnapshot:
        with self._connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM model_budget_snapshots WHERE run_id=?", (self.run_id,)
            ).fetchone()
            if row is None:
                raise BudgetNotFoundError(f"run:{self.run_id}")
            lease_rows = connection.execute(
                "SELECT * FROM model_budget_leases WHERE run_id=? ORDER BY created_at,lease_id",
                (self.run_id,),
            ).fetchall()
            role_rows = connection.execute(
                "SELECT role,requested,extended FROM model_budget_role_totals "
                "WHERE run_id=?",
                (self.run_id,),
            ).fetchall()
            used = self._used_count(connection)
            verifier_used = self._used_count(connection, BudgetRole.VERIFIER)
            borrowed = int(
                connection.execute(
                    "SELECT COUNT(*) FROM model_budget_leases WHERE run_id=? AND borrowed=1 "
                    "AND status IN ('started','committed','recovered')",
                    (self.run_id,),
                ).fetchone()[0]
            )
        floor = int(row["verifier_floor"])
        active = int(row["active_limit"])
        initial = int(row["initial_limit"])
        leases = tuple(self._lease(item) for item in lease_rows)
        persisted_totals = {
            BudgetRole(str(item["role"])): (int(item["requested"]), int(item["extended"]))
            for item in role_rows
        }
        role_totals = tuple(
            BudgetRoleTotals(
                role=role,
                requested=persisted_totals.get(
                    role, (sum(lease.role is role for lease in leases), 0)
                )[0],
                used=sum(
                    lease.role is role
                    and lease.status
                    in {LeaseStatus.STARTED, LeaseStatus.COMMITTED, LeaseStatus.RECOVERED}
                    for lease in leases
                ),
                reserved=sum(
                    lease.role is role and lease.status is LeaseStatus.RESERVED
                    for lease in leases
                ),
                borrowed=sum(
                    lease.role is role
                    and lease.borrowed
                    and lease.status
                    in {LeaseStatus.STARTED, LeaseStatus.COMMITTED, LeaseStatus.RECOVERED}
                    for lease in leases
                ),
                extended=persisted_totals.get(role, (0, 0))[1],
            )
            for role in BudgetRole
        )
        return BudgetSnapshot(
            self.run_id,
            initial,
            active,
            int(row["hard_limit"]),
            int(row["requested"]),
            used,
            floor,
            max(0, floor - verifier_used),
            borrowed,
            active - initial,
            int(row["extension_count"]),
            int(row["max_extensions"]),
            leases,
            role_totals,
            str(row["final_stop_reason"]),
        )

    def _used_count(
        self, connection: sqlite3.Connection, role: BudgetRole | None = None
    ) -> int:
        if role is None:
            row = connection.execute(
                "SELECT COUNT(*) FROM model_budget_leases WHERE run_id=? "
                "AND status IN ('started','committed','recovered')",
                (self.run_id,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT COUNT(*) FROM model_budget_leases WHERE run_id=? AND role=? "
                "AND status IN ('started','committed','recovered')",
                (self.run_id, role.value),
            ).fetchone()
        return int(row[0])

    @staticmethod
    def _lease(row: sqlite3.Row) -> BudgetLease:
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
