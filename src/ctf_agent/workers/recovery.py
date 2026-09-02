"""Durable decision and report checkpoint persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ctf_agent.security import protect_file, redact_persisted_value
from ctf_agent.workers.models import WorkerDecision, WorkerReport

if TYPE_CHECKING:
    from ctf_agent.budget_types import BudgetLease
    from ctf_agent.workers.core import WorkerCore


def persist_decision(
    worker: WorkerCore,
    step: int,
    lease: BudgetLease | None,
    decision: WorkerDecision,
) -> None:
    checkpoint = worker._durable_checkpoint
    if checkpoint is None or worker.checkpoint_store is None:
        return
    request_id = lease.request_id if lease is not None else None
    safe_decision = decision.model_copy(update={"flag_candidates": []})
    decision_path = worker.workspace.artifacts_dir / f"recovery-step-{step}-decision.json"
    durable_decision = WorkerDecision.model_validate(
        redact_persisted_value(decision.model_dump(mode="json"))
    )
    decision_path.write_text(durable_decision.model_dump_json(), encoding="utf-8")
    protect_file(decision_path)
    worker._durable_checkpoint = worker.checkpoint_store.save(
        checkpoint.model_copy(
            update={
                "pending_step": step,
                "pending_request_id": str(request_id) if request_id is not None else None,
                "pending_decision_json": safe_decision.model_dump_json(),
                "pending_decision_path": str(decision_path),
                "completed_report_json": None,
                "completed_report_path": None,
            }
        )
    )


def persist_report(worker: WorkerCore, report: WorkerReport) -> None:
    checkpoint = worker._durable_checkpoint
    if checkpoint is None or worker.checkpoint_store is None:
        return
    safe_report = report.model_copy(update={"flag_candidates": []})
    report_path = worker.workspace.artifacts_dir / f"recovery-step-{report.step}-report.json"
    durable_report = WorkerReport.model_validate(
        redact_persisted_value(report.model_dump(mode="json"))
    )
    report_path.write_text(durable_report.model_dump_json(), encoding="utf-8")
    protect_file(report_path)
    worker._durable_checkpoint = worker.checkpoint_store.save(
        checkpoint.model_copy(
            update={
                "completed_report_json": safe_report.model_dump_json(),
                "completed_report_path": str(report_path),
            }
        )
    )
