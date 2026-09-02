"""Bounded continuation adapter for the controlled worker loop."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, assert_never

from ctf_agent.lanes.model import (
    CandidateHistoryEntry,
    LaneCheckpoint,
    LaneStatus,
    ProvenancedFact,
)
from ctf_agent.schemas import FlagCandidate

if TYPE_CHECKING:
    from ctf_agent.workers.core import WorkerCore, WorkerReport, WorkerSliceResult


async def run_worker_slice(
    worker: WorkerCore,
    task: str,
    context: Mapping[str, Any] | None,
    *,
    checkpoint: LaneCheckpoint,
    max_steps: int | None,
    deadline: datetime | None,
) -> WorkerSliceResult:
    """Restore worker dedupe state, execute a bound, and return updated durable truth."""
    from ctf_agent.workers.core import WorkerExecutionError, WorkerSliceResult

    if max_steps is None and deadline is None:
        raise WorkerExecutionError("run_slice requires max_steps or deadline")
    slice_steps = max_steps or worker.budget.max_steps
    if slice_steps < 1:
        raise WorkerExecutionError("run_slice max_steps must be positive")
    wall_seconds = worker.budget.max_wall_time_seconds
    if deadline is not None:
        normalized = deadline if deadline.tzinfo is not None else deadline.replace(tzinfo=UTC)
        wall_seconds = min(wall_seconds, (normalized - datetime.now(UTC)).total_seconds())
        if wall_seconds <= 0:
            raise WorkerExecutionError("run_slice deadline has passed")

    worker._seen_commands = set(checkpoint.command_fingerprints)
    worker._seen_outputs = set(checkpoint.output_fingerprints)
    worker._seen_facts = set(checkpoint.verified_facts)
    worker._seen_candidates = {item.value_sha256 for item in checkpoint.candidate_history}
    worker._written_hashes = dict(checkpoint.written_file_hashes)
    worker._workspace_generation = checkpoint.workspace_generation
    worker._step_offset = checkpoint.step_index
    worker._initial_no_progress = checkpoint.no_progress_steps
    worker._durable_checkpoint = checkpoint
    original_budget = worker.budget
    worker.budget = original_budget.model_copy(
        update={
            "max_steps": min(slice_steps, original_budget.max_steps),
            "max_model_calls": min(slice_steps, original_budget.max_model_calls),
            "max_commands": max(0, original_budget.max_commands - checkpoint.commands_run),
            "max_http_requests": max(
                0, original_budget.max_http_requests - checkpoint.http_requests_run
            ),
            "max_wall_time_seconds": wall_seconds,
        }
    )
    continuation = dict(context or {})
    continuation["lane_checkpoint"] = {
        "step_index": checkpoint.step_index,
        "verified_facts": list(checkpoint.verified_facts),
        "facts": [item.model_dump(mode="json") for item in checkpoint.facts],
        "failed_approaches": list(checkpoint.failed_approaches),
        "artifacts": list(checkpoint.artifacts),
        "next_action": checkpoint.next_action,
        "workspace_generation": checkpoint.workspace_generation,
    }
    try:
        result = await worker.run(task, continuation)
    finally:
        worker.budget = original_budget
        worker._step_offset = 0
        worker._initial_no_progress = 0

    status = _slice_status(result.status, result.message, result.flag_candidates)
    fact_values = _unique(
        (
            *checkpoint.verified_facts,
            *(worker.sanitizer.sanitize(item).text for item in result.facts),
        )
    )
    facts = tuple(
        ProvenancedFact(
            fact=fact,
            source=(
                "command"
                if any(
                    fact in report.facts and report.action == "run"
                    for report in result.reports
                )
                else "model"
            ),
            artifact=next(
                (report.stdout_artifact for report in result.reports
                 if fact in report.facts and report.stdout_artifact), None
            ),
            command=next(
                (tuple(report.argv) for report in result.reports
                 if fact in report.facts and report.argv), ()
            ),
            evidence_sha256=hashlib.sha256(fact.encode()).hexdigest(),
            status="validated" if any(
                fact in report.facts and report.action == "run" and report.status == "ok"
                for report in result.reports
            ) else "untrusted",
            sequence=index,
        )
        for index, fact in enumerate(fact_values, 1)
    )
    failures = _unique(
        (
            *checkpoint.failed_approaches,
            *(
                worker.sanitizer.sanitize(report.message).text
                for report in result.reports
                if report.status in {"failed", "timeout"} and report.message
            ),
        )
    )
    artifacts = _unique((*checkpoint.artifacts, *_report_artifacts(result.reports)))
    histories = list(checkpoint.candidate_history)
    seen_candidates = {item.value_sha256 for item in histories}
    for candidate in result.flag_candidates:
        value_sha256 = hashlib.sha256(candidate.value.encode()).hexdigest()
        if value_sha256 in seen_candidates:
            continue
        seen_candidates.add(value_sha256)
        histories.append(
            CandidateHistoryEntry(
                value_sha256=value_sha256,
                source_artifact=candidate.source_artifact,
                source_location=candidate.source_location,
                confidence=candidate.confidence,
                observed_step=checkpoint.step_index + result.steps,
            )
        )
    latest_message = next(
        (report.message for report in reversed(result.reports) if report.message),
        result.message,
    )
    no_progress = checkpoint.no_progress_steps
    for report in result.reports:
        no_progress = 0 if report.made_progress else no_progress + 1
    durable_checkpoint = worker._durable_checkpoint or checkpoint
    updated = durable_checkpoint.model_copy(
        update={
            "status": status,
            "step_index": checkpoint.step_index + result.steps,
            "verified_facts": tuple(item.fact for item in facts),
            "facts": facts,
            "failed_approaches": failures,
            "artifacts": artifacts,
            "candidate_history": tuple(histories),
            "next_action": worker.sanitizer.sanitize(latest_message).text,
            "command_fingerprints": tuple(sorted(worker._seen_commands)),
            "output_fingerprints": tuple(sorted(worker._seen_outputs)),
            "written_file_hashes": dict(worker._written_hashes),
            "no_progress_steps": no_progress,
            "commands_run": checkpoint.commands_run + result.commands_run,
            "http_requests_run": checkpoint.http_requests_run + result.http_requests_run,
            "pending_step": None,
            "pending_request_id": None,
            "pending_decision_json": None,
            "pending_decision_path": None,
            "completed_report_json": None,
            "completed_report_path": None,
            "updated_at": datetime.now(UTC),
        }
    )
    return WorkerSliceResult(status=status, checkpoint=updated, result=result)


def _slice_status(
    worker_status: Literal["finished", "budget_exhausted", "error"],
    message: str,
    candidates: list[FlagCandidate],
) -> LaneStatus:
    match worker_status:
        case "finished":
            return LaneStatus.SOLVED if candidates else LaneStatus.STALLED
        case "budget_exhausted":
            if "no progress" in message or (
                "budget exhausted" in message and message != "step budget exhausted"
            ):
                return LaneStatus.STALLED
            return LaneStatus.PROGRESS
        case "error":
            return LaneStatus.FAILED
        case unreachable:
            assert_never(unreachable)


def _report_artifacts(reports: list[WorkerReport]) -> tuple[str, ...]:
    paths: list[str] = []
    for report in reports:
        paths.extend(
            value
            for value in (
                report.written_path,
                report.stdout_artifact,
                report.stderr_artifact,
                report.metadata_artifact,
                report.response_artifact,
            )
            if value is not None
        )
    return tuple(paths)


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
