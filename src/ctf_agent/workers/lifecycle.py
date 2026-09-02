"""Budgeted worker lifecycle orchestration."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, assert_never

from ctf_agent.budget_types import (
    BudgetExhaustedError,
    BudgetPurpose,
    BudgetRequest,
    BudgetRequestId,
    BudgetRole,
    LeaseStatus,
)
from ctf_agent.models.base import ModelBackendError
from ctf_agent.workers.action import execute_action
from ctf_agent.workers.artifacts import aggregate_reports
from ctf_agent.workers.models import WorkerDecision, WorkerReport, WorkerResult

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ctf_agent.workers.core import WorkerCore


async def run_worker(
    worker: WorkerCore,
    task: str,
    context: Mapping[str, Any] | None = None,
) -> WorkerResult:
    started = time.monotonic()
    reports: list[WorkerReport] = []
    model_calls = 0
    commands_run = 0
    http_requests_run = 0
    first_step = 1
    recovered_decision: WorkerDecision | None = None
    checkpoint = worker._durable_checkpoint
    if checkpoint is not None and checkpoint.completed_report_json is not None:
        report_payload = (
            Path(checkpoint.completed_report_path).read_text(encoding="utf-8")
            if checkpoint.completed_report_path is not None
            else checkpoint.completed_report_json
        )
        report = WorkerReport.model_validate_json(report_payload)
        reports.append(report)
        model_calls = 1
        commands_run = int(report.action == "run" and report.status != "skipped")
        http_requests_run = int(
            report.action == "http_request" and report.status != "skipped"
        )
        first_step = 2
        if report.action == "finish":
            aggregates = aggregate_reports(reports)
            return WorkerResult(
                status="finished",
                message=report.message or "worker finished",
                reports=reports,
                steps=1,
                model_calls=model_calls,
                commands_run=commands_run,
                http_requests_run=http_requests_run,
                elapsed_seconds=round(time.monotonic() - started, 6),
                facts=aggregates["facts"],
                flag_candidates=aggregates["flag_candidates"],
                written_files=aggregates["written_files"],
            )
    elif checkpoint is not None and checkpoint.pending_decision_json is not None:
        decision_payload = (
            Path(checkpoint.pending_decision_path).read_text(encoding="utf-8")
            if checkpoint.pending_decision_path is not None
            else checkpoint.pending_decision_json
        )
        recovered_decision = WorkerDecision.model_validate_json(decision_payload)
    no_progress = worker._initial_no_progress
    context_dict = dict(context or {})
    active_model_budget = worker.model_budget

    for step in range(first_step, worker.budget.max_steps + 1):
        absolute_step = worker._step_offset + step
        elapsed = time.monotonic() - started
        if elapsed >= worker.budget.max_wall_time_seconds:
            return worker._budget_result(
                "wall time budget exhausted",
                reports,
                started,
                model_calls,
                commands_run,
                http_requests_run,
            )
        if model_calls >= worker.budget.max_model_calls:
            return worker._budget_result(
                "model call budget exhausted",
                reports,
                started,
                model_calls,
                commands_run,
                http_requests_run,
            )

        lease = None
        if recovered_decision is None and active_model_budget is not None:
            attempt = 1
            while True:
                base_request_id = f"{worker.budget_request_prefix}:{absolute_step}"
                request_id = (
                    base_request_id
                    if attempt == 1
                    else f"{base_request_id}:attempt:{attempt}"
                )
                request = BudgetRequest(
                    BudgetRole.SOLVER,
                    BudgetPurpose.SOLVE,
                    BudgetRequestId(request_id),
                )
                try:
                    lease = await active_model_budget.acquire(request)
                except BudgetExhaustedError as exc:
                    return worker._budget_result(
                        str(exc),
                        reports,
                        started,
                        model_calls,
                        commands_run,
                        http_requests_run,
                    )
                match lease.status:
                    case LeaseStatus.RESERVED:
                        break
                    case (
                        LeaseStatus.STARTED
                        | LeaseStatus.COMMITTED
                        | LeaseStatus.RELEASED
                        | LeaseStatus.RECOVERED
                    ):
                        attempt += 1
                    case unreachable:
                        assert_never(unreachable)
            worker._hit_failpoint("acquire")
            await active_model_budget.start(lease.lease_id)
            worker._hit_failpoint("start")
        model_calls += int(recovered_decision is None)
        request_payload: dict[str, Any] = {
            "role": "solver",
            "worker_step": absolute_step,
            "request_index": model_calls,
        }
        if lease is not None:
            request_payload["request_id"] = lease.request_id
        worker._emit("model.request", request_payload)
        try:
            decision = recovered_decision or await worker._next_decision(
                task, context_dict, reports
            )
            if recovered_decision is None:
                worker._persist_decision(absolute_step, lease, decision)
                worker._hit_failpoint("model_completion")
        except (ModelBackendError, ValueError) as exc:
            if lease is not None and active_model_budget is not None:
                await active_model_budget.commit(lease.lease_id)
            worker._emit(
                "model.failure",
                {
                    "role": "solver",
                    "worker_step": absolute_step,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            return WorkerResult(
                status="error",
                message=f"model decision failed: {type(exc).__name__}: {exc}",
                reports=reports,
                steps=len(reports),
                model_calls=model_calls,
                commands_run=commands_run,
                http_requests_run=http_requests_run,
                elapsed_seconds=round(time.monotonic() - started, 6),
                **aggregate_reports(reports),
            )
        if lease is not None and active_model_budget is not None:
            await active_model_budget.commit(lease.lease_id)
        recovered_decision = None
        terminal, commands_run, http_requests_run = await execute_action(
            worker,
            decision,
            reports,
            absolute_step=absolute_step,
            step=step,
            started=started,
            model_calls=model_calls,
            commands_run=commands_run,
            http_requests_run=http_requests_run,
        )
        if terminal is not None:
            return terminal

        worker._persist_report(reports[-1])
        worker._hit_failpoint("action_completion")
        no_progress = 0 if reports[-1].made_progress else no_progress + 1
        if no_progress >= worker.budget.max_no_progress_steps:
            return worker._budget_result(
                "no progress budget exhausted",
                reports,
                started,
                model_calls,
                commands_run,
                http_requests_run,
            )

    return worker._budget_result(
        "step budget exhausted",
        reports,
        started,
        model_calls,
        commands_run,
        http_requests_run,
    )
