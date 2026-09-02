"""Persistent elastic model budget broker and inspection CLI."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, assert_never

import typer

from ctf_agent.budget_recovery import ModelBudgetRecovery
from ctf_agent.budget_store import ModelBudgetStore
from ctf_agent.budget_types import (
    BudgetExhaustedError,
    BudgetLease,
    BudgetLeaseId,
    BudgetPolicy,
    BudgetRequest,
    BudgetSnapshot,
    LeaseStatus,
    ProgressEvidence,
)

type BudgetEventPayload = dict[str, str | int | bool]
type BudgetObserver = Callable[[str, BudgetEventPayload], None]


class ModelBudgetBroker:
    """Expose async lease lifecycle operations over transactional SQLite state."""

    def __init__(self, database: Path, run_id: str, *, recover: bool) -> None:
        self.database = database
        self.run_id = run_id
        self._store = ModelBudgetStore(database, run_id)
        self._observer: BudgetObserver | None = None
        if recover:
            ModelBudgetRecovery(database, run_id).recover_started()

    @classmethod
    def create(
        cls, database: Path, run_id: str, policy: BudgetPolicy
    ) -> ModelBudgetBroker:
        store = ModelBudgetStore(database, run_id)
        store.create(policy)
        return cls(database, run_id, recover=True)

    @classmethod
    def open(
        cls, database: Path, run_id: str, *, recover: bool = True
    ) -> ModelBudgetBroker:
        return cls(database, run_id, recover=recover)

    def observe(self, observer: BudgetObserver) -> None:
        self._observer = observer

    def reconcile_legacy(self, requests: tuple[BudgetRequest, ...]) -> int:
        return ModelBudgetRecovery(self.database, self.run_id).reconcile_legacy(requests)

    def reconcile_events(self, events: Sequence[Mapping[str, Any]]) -> int:
        return ModelBudgetRecovery(self.database, self.run_id).reconcile_events(events)

    async def acquire(self, request: BudgetRequest) -> BudgetLease:
        result = self._store.acquire(request)
        match result:
            case BudgetLease() as lease:
                self._emit(
                    "budget.acquired",
                    {
                        "lease_id": lease.lease_id,
                        "request_id": lease.request_id,
                        "role": lease.role.value,
                        "purpose": lease.purpose.value,
                    },
                )
                return lease
            case BudgetExhaustedError() as denied:
                self._emit(
                    "budget.denied",
                    {
                        "request_id": denied.request_id,
                        "role": denied.role.value,
                        "purpose": denied.purpose.value,
                        "reason": denied.reason,
                    },
                )
                raise denied
            case unreachable:
                assert_never(unreachable)

    async def start(self, lease_id: BudgetLeaseId) -> BudgetLease:
        return self._transition(lease_id, LeaseStatus.RESERVED, LeaseStatus.STARTED)

    async def commit(self, lease_id: BudgetLeaseId) -> BudgetLease:
        return self._transition(lease_id, LeaseStatus.STARTED, LeaseStatus.COMMITTED)

    async def release(self, lease_id: BudgetLeaseId) -> BudgetLease:
        return self._transition(lease_id, LeaseStatus.RESERVED, LeaseStatus.RELEASED)

    def extend(self, evidence: ProgressEvidence) -> int:
        if not isinstance(evidence, ProgressEvidence):
            raise TypeError("budget extension requires ProgressEvidence")
        added = self._store.extend(evidence)
        if added:
            self._emit(
                "budget.extended",
                {
                    "evidence_facts": len(evidence.facts),
                    "evidence_artifacts": len(evidence.artifacts),
                    "evidence_candidates": len(evidence.candidates),
                    "extended": added,
                },
            )
        return added

    def snapshot(self) -> BudgetSnapshot:
        return self._store.snapshot()

    def finish(self, stop_reason: str) -> None:
        self._store.finish(stop_reason)
        self._emit("budget.finished", {"final_stop_reason": stop_reason})

    def _transition(
        self, lease_id: BudgetLeaseId, expected: LeaseStatus, target: LeaseStatus
    ) -> BudgetLease:
        lease = self._store.transition(lease_id, expected, target)
        self._emit(
            f"budget.{target.value}",
            {
                "lease_id": lease.lease_id,
                "request_id": lease.request_id,
                "role": lease.role.value,
                "purpose": lease.purpose.value,
            },
        )
        return lease

    def _emit(self, event_type: str, payload: BudgetEventPayload) -> None:
        if self._observer is not None:
            self._observer(event_type, payload)


app = typer.Typer(help="Inspect persistent model-call budgets.")


@app.callback()
def budget_cli() -> None:
    """Manage persistent model-call budget state."""


@app.command("inspect")
def inspect_budget(
    database: Annotated[
        Path,
        typer.Option("--database", exists=True, dir_okay=False),
    ],
    *,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with sqlite3.connect(database) as connection:
        has_budget = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='model_budget_snapshots'"
        ).fetchone()
        rows = (
            connection.execute(
                "SELECT run_id FROM model_budget_snapshots ORDER BY run_id"
            ).fetchall()
            if has_budget is not None
            else []
        )
    payload = {
        "database": str(database),
        "runs": [
            ModelBudgetBroker.open(database, str(row[0]), recover=False).snapshot().to_dict()
            for row in rows
        ],
    }
    typer.echo(json.dumps(payload, indent=None if json_output else 2, sort_keys=True))


if __name__ == "__main__":
    app()
