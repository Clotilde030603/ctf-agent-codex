"""Worker decision action dispatch and file writes."""

from __future__ import annotations

import hashlib
import time
from typing import TYPE_CHECKING

from ctf_agent.workers.artifacts import aggregate_reports, findings_to_dict
from ctf_agent.workers.models import (
    WorkerDecision,
    WorkerExecutionError,
    WorkerReport,
    WorkerResult,
)

if TYPE_CHECKING:
    from ctf_agent.workers.core import WorkerCore


async def execute_action(
    worker: WorkerCore,
    decision: WorkerDecision,
    reports: list[WorkerReport],
    *,
    absolute_step: int,
    step: int,
    started: float,
    model_calls: int,
    commands_run: int,
    http_requests_run: int,
) -> tuple[WorkerResult | None, int, int]:
    try:
        if decision.action == "finish":
            progress = worker._capture_decision_progress(decision)
            reports.append(
                WorkerReport(
                    step=absolute_step,
                    action="finish",
                    status="ok",
                    message=decision.message,
                    facts=decision.facts,
                    flag_candidates=decision.flag_candidates,
                    made_progress=progress,
                )
            )
            worker._persist_report(reports[-1])
            worker._hit_failpoint("action_completion")
            aggregates = aggregate_reports(reports)
            return (
                WorkerResult(
                    status="finished",
                    message=decision.message or "worker finished",
                    reports=reports,
                    steps=step,
                    model_calls=model_calls,
                    commands_run=commands_run,
                    http_requests_run=http_requests_run,
                    elapsed_seconds=round(time.monotonic() - started, 6),
                    facts=aggregates["facts"],
                    flag_candidates=aggregates["flag_candidates"],
                    written_files=aggregates["written_files"],
                ),
                commands_run,
                http_requests_run,
            )
        if decision.action == "write_file":
            reports.append(write_file(worker, absolute_step, decision))
        elif decision.action == "tcp_connect":
            reports.append(
                WorkerReport(
                    step=absolute_step,
                    action="tcp_connect",
                    status="failed",
                    message="tcp-controller capability is unavailable",
                )
            )
        elif decision.action == "http_request":
            if http_requests_run >= worker.budget.max_http_requests:
                return (
                    worker._budget_result(
                        "HTTP request budget exhausted",
                        reports,
                        started,
                        model_calls,
                        commands_run,
                        http_requests_run,
                    ),
                    commands_run,
                    http_requests_run,
                )
            report = await worker._http_request(absolute_step, decision)
            reports.append(report)
            if report.status != "skipped":
                http_requests_run += 1
        else:
            if commands_run >= worker.budget.max_commands:
                return (
                    worker._budget_result(
                        "command budget exhausted",
                        reports,
                        started,
                        model_calls,
                        commands_run,
                        http_requests_run,
                    ),
                    commands_run,
                    http_requests_run,
                )
            report = await worker._run_command(absolute_step, decision)
            reports.append(report)
            if report.status != "skipped":
                commands_run += 1
    except WorkerExecutionError as exc:
        progress = worker._capture_decision_progress(decision)
        reports.append(
            WorkerReport(
                step=absolute_step,
                action=decision.action,
                status="failed",
                message=str(exc),
                argv=decision.argv,
                facts=decision.facts,
                flag_candidates=decision.flag_candidates,
                made_progress=progress,
            )
        )
    return None, commands_run, http_requests_run


def write_file(
    worker: WorkerCore, step: int, decision: WorkerDecision
) -> WorkerReport:
    assert decision.path is not None
    assert decision.content is not None
    sanitized = worker.sanitizer.sanitize(decision.content)
    target = worker.workspace.resolve_relative(decision.path)
    content_hash = hashlib.sha256(sanitized.text.encode("utf-8")).hexdigest()
    previous_hash = worker._written_hashes.get(str(target))
    target = worker.workspace.write_relative_file(decision.path, sanitized.text)
    changed = previous_hash != content_hash
    worker._written_hashes[str(target)] = content_hash
    progress = changed or worker._capture_decision_progress(decision)
    return WorkerReport(
        step=step,
        action="write_file",
        status="ok",
        message=decision.message,
        written_path=str(target),
        facts=decision.facts,
        flag_candidates=decision.flag_candidates,
        made_progress=progress,
        redacted=sanitized.redacted,
        sanitizer_findings=findings_to_dict(sanitized.findings),
    )
