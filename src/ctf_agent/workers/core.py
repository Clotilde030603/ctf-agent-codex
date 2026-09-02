"""Public facade for the controlled worker action loop."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from ctf_agent.budget_types import BudgetLease, ModelBudgetLeaser
from ctf_agent.evidence.sanitizer import SecretSanitizer
from ctf_agent.execution_receipts import ExecutionReceiptStore
from ctf_agent.ingestion.session import ScopedAsyncSession
from ctf_agent.lanes.model import LaneCheckpoint
from ctf_agent.lanes.store import LaneCheckpointStore
from ctf_agent.models.base import ModelBackend
from ctf_agent.workers.action import write_file
from ctf_agent.workers.artifacts import LaneWorkspace, aggregate_reports
from ctf_agent.workers.command import (
    CommandPolicy,
    command_fingerprint,
    execution_command,
    run_command,
)
from ctf_agent.workers.decision import capture_decision_progress, emit, next_decision
from ctf_agent.workers.http_action import http_request, read_upload
from ctf_agent.workers.lifecycle import run_worker
from ctf_agent.workers.models import (
    MultipartUpload,
    WorkerBudget,
    WorkerDecision,
    WorkerExecutionError,
    WorkerReport,
    WorkerResult,
    WorkerSliceResult,
)
from ctf_agent.workers.recovery import persist_decision, persist_report


class WorkerCore:
    """Coordinate model decisions through bounded, controlled worker actions."""

    def __init__(
        self,
        backend: ModelBackend,
        workspace: LaneWorkspace,
        *,
        budget: WorkerBudget | None = None,
        policy: CommandPolicy | None = None,
        sanitizer: SecretSanitizer | None = None,
        model_budget: ModelBudgetLeaser | None = None,
        budget_request_prefix: str = "solver",
        http_session: ScopedAsyncSession | None = None,
        event_observer: Callable[[str, Mapping[str, Any]], None] | None = None,
        checkpoint_store: LaneCheckpointStore | None = None,
        execution_receipts: ExecutionReceiptStore | None = None,
        lane_id: str | None = None,
        failpoint: Callable[[str], None] | None = None,
    ) -> None:
        self.backend = backend
        self.workspace = workspace
        self.budget = budget or WorkerBudget()
        self.policy = policy or CommandPolicy()
        self.sanitizer = sanitizer or SecretSanitizer()
        self.model_budget = model_budget
        self.budget_request_prefix = budget_request_prefix
        self.http_session = http_session
        self.event_observer = event_observer
        self.checkpoint_store = checkpoint_store
        self.execution_receipts = execution_receipts
        self.lane_id = lane_id
        self.failpoint = failpoint
        self._durable_checkpoint: LaneCheckpoint | None = None
        self._seen_commands: set[str] = set()
        self._seen_outputs: set[str] = set()
        self._seen_facts: set[str] = set()
        self._seen_candidates: set[str] = set()
        self._written_hashes: dict[str, str] = {}
        self._workspace_generation = 0
        self._step_offset = 0
        self._initial_no_progress = 0

    async def run(
        self, task: str, context: Mapping[str, Any] | None = None
    ) -> WorkerResult:
        return await run_worker(self, task, context)

    async def run_slice(
        self,
        task: str,
        context: Mapping[str, Any] | None = None,
        *,
        checkpoint: LaneCheckpoint,
        max_steps: int | None = None,
        deadline: datetime | None = None,
    ) -> WorkerSliceResult:
        """Execute and checkpoint one bounded continuation slice."""
        from ctf_agent.workers.slice import run_worker_slice

        return await run_worker_slice(
            self,
            task,
            context,
            checkpoint=checkpoint,
            max_steps=max_steps,
            deadline=deadline,
        )

    async def _next_decision(
        self, task: str, context: dict[str, Any], reports: list[WorkerReport]
    ) -> WorkerDecision:
        return await next_decision(self, task, context, reports)

    def _write_file(self, step: int, decision: WorkerDecision) -> WorkerReport:
        return write_file(self, step, decision)

    async def _run_command(
        self, step: int, decision: WorkerDecision
    ) -> WorkerReport:
        return await run_command(self, step, decision)

    async def _http_request(
        self, step: int, decision: WorkerDecision
    ) -> WorkerReport:
        return await http_request(self, step, decision)

    def _read_upload(self, relative_path: str) -> bytes:
        return read_upload(self, relative_path)

    def _capture_decision_progress(self, decision: WorkerDecision) -> bool:
        return capture_decision_progress(self, decision)

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        emit(self, event_type, payload)

    def _execution_command(self, argv: Sequence[str]) -> list[str]:
        return execution_command(self, argv)

    def _persist_decision(
        self,
        step: int,
        lease: BudgetLease | None,
        decision: WorkerDecision,
    ) -> None:
        persist_decision(self, step, lease, decision)

    def _persist_report(self, report: WorkerReport) -> None:
        persist_report(self, report)

    def _hit_failpoint(self, phase: str) -> None:
        if self.failpoint is not None:
            self.failpoint(phase)

    def _budget_result(
        self,
        message: str,
        reports: list[WorkerReport],
        started: float,
        model_calls: int,
        commands_run: int,
        http_requests_run: int,
    ) -> WorkerResult:
        return WorkerResult(
            status="budget_exhausted",
            message=message,
            reports=reports,
            steps=len(reports),
            model_calls=model_calls,
            commands_run=commands_run,
            http_requests_run=http_requests_run,
            elapsed_seconds=round(time.monotonic() - started, 6),
            **aggregate_reports(reports),
        )


__all__ = [
    "CommandPolicy",
    "LaneWorkspace",
    "MultipartUpload",
    "WorkerBudget",
    "WorkerCore",
    "WorkerDecision",
    "WorkerExecutionError",
    "WorkerReport",
    "WorkerResult",
    "WorkerSliceResult",
    "command_fingerprint",
]
