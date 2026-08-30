from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator

from ctf_agent.config import DEFAULT_CTF_TOOL_IMAGE
from ctf_agent.evidence.sanitizer import SecretSanitizer
from ctf_agent.ingestion.session import ScopedAsyncSession
from ctf_agent.models.base import ModelBackend, ModelBackendError, ModelRequest
from ctf_agent.schemas import FlagCandidate

WorkerAction = Literal["run", "write_file", "http_request", "finish"]
WorkerStatus = Literal["finished", "budget_exhausted", "error"]


class MultipartUpload(BaseModel):
    field_name: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1)
    filename: str | None = None
    content_type: str = "application/octet-stream"


class WorkerDecision(BaseModel):
    """Single model-selected worker action.

    The schema intentionally allows exactly four actions:
    - run an argv vector from an allowlist,
    - write one relative file in the lane workspace,
    - issue a request through a host-scoped HTTP session,
    - finish with a status message.
    """

    action: WorkerAction
    argv: list[str] = Field(default_factory=list)
    path: str | None = None
    content: str | None = None
    method: Literal[
        "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"
    ] | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    query_params: dict[str, str | list[str]] = Field(default_factory=dict)
    json_body: dict[str, Any] | list[Any] | None = None
    form_body: dict[str, str] = Field(default_factory=dict)
    multipart: list[MultipartUpload] = Field(default_factory=list)
    message: str = ""
    facts: list[str] = Field(default_factory=list)
    flag_candidates: list[FlagCandidate] = Field(default_factory=list)

    @field_validator("argv")
    @classmethod
    def reject_shell_vectors(cls, value: list[str]) -> list[str]:
        if any(not isinstance(item, str) or not item for item in value):
            raise ValueError("argv entries must be non-empty strings")
        forbidden = {"sh", "bash", "zsh", "fish", "cmd", "powershell", "pwsh"}
        if value and Path(value[0]).name in forbidden:
            raise ValueError("shell executables are not allowed")
        return value

    @model_validator(mode="after")
    def validate_action_fields(self) -> WorkerDecision:
        sensitive_headers = {"authorization", "cookie", "proxy-authorization"}
        if sensitive_headers.intersection(name.lower() for name in self.headers):
            raise ValueError("model-supplied credential headers are not allowed")
        request_fields_present = bool(
            self.method
            or self.url
            or self.headers
            or self.body is not None
            or self.query_params
            or self.json_body is not None
            or self.form_body
            or self.multipart
        )
        if self.action == "run":
            if not self.argv:
                raise ValueError("run action requires argv")
            if (
                self.path is not None
                or self.content is not None
                or request_fields_present
            ):
                raise ValueError("run action may not include unrelated fields")
        elif self.action == "write_file":
            if not self.path:
                raise ValueError("write_file action requires path")
            if self.content is None:
                raise ValueError("write_file action requires content")
            if (
                self.argv
                or request_fields_present
            ):
                raise ValueError("write_file action may not include unrelated fields")
        elif self.action == "http_request":
            if self.method is None or self.url is None:
                raise ValueError("http_request action requires method and url")
            if self.argv or self.path is not None or self.content is not None:
                raise ValueError("http_request may not include argv/path/content")
            body_kinds = sum(
                (
                    self.body is not None,
                    self.json_body is not None,
                    bool(self.form_body),
                    bool(self.multipart),
                )
            )
            if body_kinds > 1:
                raise ValueError("HTTP action accepts only one body representation")
            if self.method in {"GET", "HEAD"} and body_kinds:
                raise ValueError(f"{self.method} action may not include a body")
        elif self.action == "finish":
            if (
                self.argv
                or self.path is not None
                or self.content is not None
                or request_fields_present
            ):
                raise ValueError("finish action may not include action-specific fields")
        return self


class WorkerBudget(BaseModel):
    max_steps: int = Field(default=8, ge=1, le=100)
    max_model_calls: int = Field(default=8, ge=1, le=100)
    max_commands: int = Field(default=4, ge=0, le=100)
    max_http_requests: int = Field(default=8, ge=0, le=100)
    max_wall_time_seconds: float = Field(default=120.0, gt=0, le=3600)
    command_timeout_seconds: float = Field(default=20.0, gt=0, le=600)
    max_no_progress_steps: int = Field(default=3, ge=1, le=50)
    stdout_limit: int = Field(default=512_000, ge=1024)
    stderr_limit: int = Field(default=512_000, ge=1024)
    response_header_limit: int = Field(default=64_000, ge=1024)
    multipart_file_limit: int = Field(default=16_000_000, ge=1024)


class CommandPolicy(BaseModel):
    allowed_argv0: set[str] = Field(
        default_factory=lambda: {
            "python",
            "python3",
            "file",
            "strings",
            "exiftool",
            "binwalk",
            "checksec",
        }
    )
    docker_image: str = DEFAULT_CTF_TOOL_IMAGE
    docker_binary: str = "docker"
    cpus: str = "1"
    memory: str = "512m"
    pids_limit: int = Field(default=128, ge=1)
    local_test_mode: bool = False

    def validate_argv(self, argv: Sequence[str]) -> None:
        if not argv:
            raise WorkerExecutionError("argv must not be empty")
        argv0 = Path(argv[0]).name
        if argv0 not in self.allowed_argv0:
            raise WorkerExecutionError(f"argv executable is not allowlisted: {argv0}")


class WorkerReport(BaseModel):
    step: int
    action: WorkerAction
    status: Literal["ok", "skipped", "failed", "timeout"]
    message: str = ""
    argv: list[str] = Field(default_factory=list)
    command: list[str] = Field(default_factory=list)
    command_fingerprint: str | None = None
    exit_code: int | None = None
    timed_out: bool = False
    stdout_artifact: str | None = None
    stderr_artifact: str | None = None
    metadata_artifact: str | None = None
    written_path: str | None = None
    method: str | None = None
    url: str | None = None
    status_code: int | None = None
    response_artifact: str | None = None
    redirect_chain: list[dict[str, Any]] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)
    flag_candidates: list[FlagCandidate] = Field(default_factory=list)
    made_progress: bool = False
    redacted: bool = False
    sanitizer_findings: dict[str, int] = Field(default_factory=dict)


class WorkerResult(BaseModel):
    status: WorkerStatus
    message: str
    reports: list[WorkerReport] = Field(default_factory=list)
    steps: int = 0
    model_calls: int = 0
    commands_run: int = 0
    http_requests_run: int = 0
    elapsed_seconds: float = 0.0
    facts: list[str] = Field(default_factory=list)
    flag_candidates: list[FlagCandidate] = Field(default_factory=list)
    written_files: list[str] = Field(default_factory=list)


class WorkerExecutionError(RuntimeError):
    pass


class SharedModelCallBudget:
    """Concurrency-safe model-call reservation shared by all solver lanes."""

    def __init__(
        self,
        limit: int,
        *,
        on_reserve: Callable[[int], None] | None = None,
    ) -> None:
        if limit < 0:
            raise ValueError("shared model call limit must be non-negative")
        self.limit = limit
        self.used = 0
        self.on_reserve = on_reserve
        self._lock = asyncio.Lock()

    async def reserve(self) -> int:
        async with self._lock:
            if self.used >= self.limit:
                raise ModelBackendError("shared model call budget exhausted")
            self.used += 1
            if self.on_reserve is not None:
                self.on_reserve(self.used)
            return self.used


class LaneWorkspace:
    def __init__(self, root: Path | str, *, challenge_files: Path | str | None = None) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir = self.root / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.challenge_files = (
            Path(challenge_files).resolve() if challenge_files is not None else None
        )

    def resolve_relative(self, relative_path: str) -> Path:
        value = Path(relative_path)
        if value.is_absolute():
            raise WorkerExecutionError(f"path must be relative: {relative_path}")
        if any(part in {"", ".", ".."} for part in value.parts):
            raise WorkerExecutionError(f"path contains unsafe segment: {relative_path}")
        target = (self.root / value).resolve()
        if target != self.root and self.root not in target.parents:
            raise WorkerExecutionError(f"path escapes lane workspace: {relative_path}")
        return target

    def write_relative_file(self, relative_path: str, content: str) -> Path:
        target = self.resolve_relative(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target


class WorkerCore:
    def __init__(
        self,
        backend: ModelBackend,
        workspace: LaneWorkspace,
        *,
        budget: WorkerBudget | None = None,
        policy: CommandPolicy | None = None,
        sanitizer: SecretSanitizer | None = None,
        shared_model_budget: SharedModelCallBudget | None = None,
        http_session: ScopedAsyncSession | None = None,
        event_observer: Callable[[str, Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.backend = backend
        self.workspace = workspace
        self.budget = budget or WorkerBudget()
        self.policy = policy or CommandPolicy()
        self.sanitizer = sanitizer or SecretSanitizer()
        self.shared_model_budget = shared_model_budget
        self.http_session = http_session
        self.event_observer = event_observer
        self._seen_commands: set[str] = set()
        self._seen_facts: set[str] = set()
        self._seen_candidates: set[str] = set()
        self._written_hashes: dict[str, str] = {}

    async def run(self, task: str, context: Mapping[str, Any] | None = None) -> WorkerResult:
        started = time.monotonic()
        reports: list[WorkerReport] = []
        model_calls = 0
        commands_run = 0
        http_requests_run = 0
        no_progress = 0
        context_dict = dict(context or {})

        for step in range(1, self.budget.max_steps + 1):
            elapsed = time.monotonic() - started
            if elapsed >= self.budget.max_wall_time_seconds:
                return self._budget_result(
                    "wall time budget exhausted",
                    reports,
                    started,
                    model_calls,
                    commands_run,
                    http_requests_run,
                )
            if model_calls >= self.budget.max_model_calls:
                return self._budget_result(
                    "model call budget exhausted",
                    reports,
                    started,
                    model_calls,
                    commands_run,
                    http_requests_run,
                )

            if self.shared_model_budget is not None:
                try:
                    await self.shared_model_budget.reserve()
                except ModelBackendError as exc:
                    return self._budget_result(
                        str(exc),
                        reports,
                        started,
                        model_calls,
                        commands_run,
                        http_requests_run,
                    )
            model_calls += 1
            self._emit(
                "model.request",
                {"role": "solver", "worker_step": step, "request_index": model_calls},
            )
            try:
                decision = await self._next_decision(task, context_dict, reports)
            except (ModelBackendError, ValueError) as exc:
                self._emit(
                    "model.failure",
                    {
                        "role": "solver",
                        "worker_step": step,
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
                    **_aggregate_reports(reports),
                )
            try:
                if decision.action == "finish":
                    progress = self._capture_decision_progress(decision)
                    reports.append(
                        WorkerReport(
                            step=step,
                            action="finish",
                            status="ok",
                            message=decision.message,
                            facts=decision.facts,
                            flag_candidates=decision.flag_candidates,
                            made_progress=progress,
                        )
                    )
                    aggregates = _aggregate_reports(reports)
                    return WorkerResult(
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
                    )
                if decision.action == "write_file":
                    report = self._write_file(step, decision)
                    reports.append(report)
                elif decision.action == "http_request":
                    if http_requests_run >= self.budget.max_http_requests:
                        return self._budget_result(
                            "HTTP request budget exhausted",
                            reports,
                            started,
                            model_calls,
                            commands_run,
                            http_requests_run,
                        )
                    report = await self._http_request(step, decision)
                    reports.append(report)
                    if report.status != "skipped":
                        http_requests_run += 1
                else:
                    if commands_run >= self.budget.max_commands:
                        return self._budget_result(
                            "command budget exhausted",
                            reports,
                            started,
                            model_calls,
                            commands_run,
                            http_requests_run,
                        )
                    report = await self._run_command(step, decision)
                    reports.append(report)
                    if report.status != "skipped":
                        commands_run += 1
            except WorkerExecutionError as exc:
                progress = self._capture_decision_progress(decision)
                reports.append(
                    WorkerReport(
                        step=step,
                        action=decision.action,
                        status="failed",
                        message=str(exc),
                        argv=decision.argv,
                        facts=decision.facts,
                        flag_candidates=decision.flag_candidates,
                        made_progress=progress,
                    )
                )
            else:
                pass

            no_progress = 0 if reports[-1].made_progress else no_progress + 1

            if no_progress >= self.budget.max_no_progress_steps:
                return self._budget_result(
                    "no progress budget exhausted",
                    reports,
                    started,
                    model_calls,
                    commands_run,
                    http_requests_run,
                )

        return self._budget_result(
            "step budget exhausted",
            reports,
            started,
            model_calls,
            commands_run,
            http_requests_run,
        )

    async def _next_decision(
        self, task: str, context: dict[str, Any], reports: list[WorkerReport]
    ) -> WorkerDecision:
        started = time.monotonic()
        response = await self.backend.complete(
            ModelRequest(
                role="worker",
                system=(
                    "You are a sandboxed CTF worker. Return one JSON object matching the "
                    "WorkerDecision schema. Never return shell strings; use argv arrays only. "
                    "Use http_request only for explicitly scoped challenge URLs; credentials "
                    "are supplied by the host session and must never be placed in headers."
                ),
                prompt=task,
                context={
                    **context,
                    "workspace": str(self.workspace.root),
                    "recent_reports": [report.model_dump(mode="json") for report in reports[-5:]],
                },
                output_schema=WorkerDecision.model_json_schema(),
            )
        )
        self._emit(
            "model.completed",
            {
                "role": "solver",
                "report_count": len(reports),
                "elapsed_seconds": round(time.monotonic() - started, 6),
            },
        )
        try:
            payload = json.loads(response.content)
        except json.JSONDecodeError as exc:
            raise ModelBackendError("worker decision must be valid JSON") from exc
        return WorkerDecision.model_validate(payload)

    def _write_file(self, step: int, decision: WorkerDecision) -> WorkerReport:
        assert decision.path is not None
        assert decision.content is not None
        sanitized = self.sanitizer.sanitize(decision.content)
        target = self.workspace.resolve_relative(decision.path)
        content_hash = hashlib.sha256(sanitized.text.encode("utf-8")).hexdigest()
        previous_hash = self._written_hashes.get(str(target))
        target = self.workspace.write_relative_file(decision.path, sanitized.text)
        changed = previous_hash != content_hash
        self._written_hashes[str(target)] = content_hash
        progress = changed or self._capture_decision_progress(decision)
        report = WorkerReport(
            step=step,
            action="write_file",
            status="ok",
            message=decision.message,
            written_path=str(target),
            facts=decision.facts,
            flag_candidates=decision.flag_candidates,
            made_progress=progress,
            redacted=sanitized.redacted,
            sanitizer_findings=_findings_to_dict(sanitized.findings),
        )
        return report

    async def _run_command(self, step: int, decision: WorkerDecision) -> WorkerReport:
        argv = decision.argv
        self.policy.validate_argv(argv)
        fingerprint = command_fingerprint(argv)
        if fingerprint in self._seen_commands:
            progress = self._capture_decision_progress(decision)
            return WorkerReport(
                step=step,
                action="run",
                status="skipped",
                message="duplicate command fingerprint",
                argv=list(argv),
                command_fingerprint=fingerprint,
                facts=decision.facts,
                flag_candidates=decision.flag_candidates,
                made_progress=progress,
            )
        self._seen_commands.add(fingerprint)

        command = self._execution_command(argv)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.workspace.root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise WorkerExecutionError(f"could not start command: {exc}") from exc
        timed_out = False
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), self.budget.command_timeout_seconds
            )
        except TimeoutError:
            timed_out = True
            process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()

        stdout = self.sanitizer.sanitize(_truncate(stdout_bytes, self.budget.stdout_limit))
        stderr = self.sanitizer.sanitize(_truncate(stderr_bytes, self.budget.stderr_limit))
        artifact_prefix = f"{step:03d}-{fingerprint[:16]}"
        stdout_path = self.workspace.artifacts_dir / f"{artifact_prefix}.stdout.txt"
        stderr_path = self.workspace.artifacts_dir / f"{artifact_prefix}.stderr.txt"
        metadata_path = self.workspace.artifacts_dir / f"{artifact_prefix}.meta.json"
        stdout_path.write_text(stdout.text, encoding="utf-8")
        stderr_path.write_text(stderr.text, encoding="utf-8")
        exit_code = process.returncode
        if timed_out:
            exit_code = 124
        metadata = {
            "argv": list(argv),
            "command": command,
            "fingerprint": fingerprint,
            "exit_code": exit_code,
            "timed_out": timed_out,
        }
        metadata_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        report = WorkerReport(
            step=step,
            action="run",
            status="timeout" if timed_out else "ok",
            argv=list(argv),
            command=command,
            command_fingerprint=fingerprint,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout_artifact=str(stdout_path),
            stderr_artifact=str(stderr_path),
            metadata_artifact=str(metadata_path),
            facts=decision.facts,
            flag_candidates=decision.flag_candidates,
            made_progress=True,
            redacted=stdout.redacted or stderr.redacted,
            sanitizer_findings=_merge_findings(stdout.findings, stderr.findings),
        )
        self._emit(
            "worker.command",
            {
                "argv": list(argv),
                "exit_code": exit_code,
                "timed_out": timed_out,
                "accepted": exit_code == 0 and not timed_out,
                "fingerprint": fingerprint,
            },
        )
        return report

    async def _http_request(self, step: int, decision: WorkerDecision) -> WorkerReport:
        if self.http_session is None:
            raise WorkerExecutionError("scoped HTTP access is not configured for this lane")
        assert decision.method is not None
        assert decision.url is not None
        serialized_body = json.dumps(
            {
                "body": decision.body,
                "json_body": decision.json_body,
                "form_body": decision.form_body,
                "multipart": [item.model_dump() for item in decision.multipart],
            },
            sort_keys=True,
            default=str,
        )
        if len(serialized_body.encode("utf-8")) > 256_000:
            raise WorkerExecutionError("HTTP request metadata/body exceeds 256000 bytes")
        fingerprint = command_fingerprint(
            [
                decision.method,
                decision.url,
                json.dumps(decision.headers, sort_keys=True),
                json.dumps(decision.query_params, sort_keys=True),
                serialized_body,
            ]
        )
        if fingerprint in self._seen_commands:
            progress = self._capture_decision_progress(decision)
            return WorkerReport(
                step=step,
                action="http_request",
                status="skipped",
                message="duplicate HTTP request fingerprint",
                method=decision.method,
                url=decision.url,
                command_fingerprint=fingerprint,
                facts=decision.facts,
                flag_candidates=decision.flag_candidates,
                made_progress=progress,
            )
        request_kwargs: dict[str, Any] = {
            "headers": decision.headers,
            "params": decision.query_params,
        }
        if decision.body is not None:
            request_kwargs["content"] = decision.body
        elif decision.json_body is not None:
            request_kwargs["json"] = decision.json_body
        elif decision.form_body:
            request_kwargs["data"] = decision.form_body
        elif decision.multipart:
            request_kwargs["files"] = [
                (
                    upload.field_name,
                    (
                        upload.filename or Path(upload.path).name,
                        self._read_upload(upload.path),
                        upload.content_type,
                    ),
                )
                for upload in decision.multipart
            ]
        try:
            response = await self.http_session.request(
                decision.method,
                decision.url,
                **request_kwargs,
            )
        except (httpx.HTTPError, ValueError) as exc:
            raise WorkerExecutionError(f"scoped HTTP request failed: {exc}") from exc
        self._seen_commands.add(fingerprint)
        body = self.sanitizer.sanitize(
            _truncate(response.content, self.budget.stdout_limit)
        )
        artifact_prefix = f"{step:03d}-{fingerprint[:16]}"
        response_path = self.workspace.artifacts_dir / f"{artifact_prefix}.http.txt"
        metadata_path = self.workspace.artifacts_dir / f"{artifact_prefix}.http.json"
        response_path.write_text(body.text, encoding="utf-8")
        safe_headers: dict[str, str] = {}
        header_bytes = 0
        for name, value in response.headers.items():
            if name.lower() in {"set-cookie", "authorization", "proxy-authenticate"}:
                continue
            sanitized_value = self.sanitizer.sanitize(value).text
            item_size = len(name.encode()) + len(sanitized_value.encode())
            if header_bytes + item_size > self.budget.response_header_limit:
                break
            safe_headers[name] = sanitized_value
            header_bytes += item_size
        chain_value = response.extensions.get("ctf_redirect_chain", [])
        redirect_chain = (
            [dict(item) for item in chain_value if isinstance(item, Mapping)]
            if isinstance(chain_value, list)
            else []
        )
        metadata_path.write_text(
            json.dumps(
                {
                    "method": decision.method,
                    "url": str(response.request.url),
                    "status_code": response.status_code,
                    "headers": safe_headers,
                    "redirect_chain": redirect_chain,
                    "fingerprint": fingerprint,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        report = WorkerReport(
            step=step,
            action="http_request",
            status="ok",
            message=decision.message,
            method=decision.method,
            url=str(response.request.url),
            status_code=response.status_code,
            command_fingerprint=fingerprint,
            response_artifact=str(response_path),
            metadata_artifact=str(metadata_path),
            redirect_chain=redirect_chain,
            facts=decision.facts,
            flag_candidates=decision.flag_candidates,
            made_progress=True,
            redacted=body.redacted,
            sanitizer_findings=_findings_to_dict(body.findings),
        )
        self._emit(
            "worker.http_request",
            {
                "method": decision.method,
                "url": str(response.request.url),
                "status_code": response.status_code,
                "accepted": response.status_code < 500,
                "redirect_count": max(0, len(redirect_chain) - 1),
                "fingerprint": fingerprint,
            },
        )
        return report

    def _read_upload(self, relative_path: str) -> bytes:
        path_value = Path(relative_path)
        if path_value.is_absolute() or ".." in path_value.parts:
            raise WorkerExecutionError("multipart path must be a safe relative path")
        if path_value.parts and path_value.parts[0] == "challenge":
            if self.workspace.challenge_files is None:
                raise WorkerExecutionError("challenge artifact root is unavailable")
            root = self.workspace.challenge_files
            path = (root / Path(*path_value.parts[1:])).resolve()
        else:
            root = self.workspace.root
            path = self.workspace.resolve_relative(relative_path)
        if root.resolve() not in path.parents or path.is_symlink() or not path.is_file():
            raise WorkerExecutionError("multipart path is outside approved workspace roots")
        if path.stat().st_size > self.budget.multipart_file_limit:
            raise WorkerExecutionError(
                f"multipart file exceeds {self.budget.multipart_file_limit} bytes"
            )
        return path.read_bytes()

    def _capture_decision_progress(self, decision: WorkerDecision) -> bool:
        progressed = False
        for fact in decision.facts:
            if fact not in self._seen_facts:
                self._seen_facts.add(fact)
                progressed = True
        for candidate in decision.flag_candidates:
            key = candidate.value
            if key not in self._seen_candidates:
                self._seen_candidates.add(key)
                self._emit(
                    "flag.candidate",
                    {
                        "candidate_sha256": hashlib.sha256(
                            candidate.value.encode()
                        ).hexdigest(),
                        "source_artifact": candidate.source_artifact,
                        "source_location": candidate.source_location,
                        "confidence": candidate.confidence,
                    },
                )
                progressed = True
        return progressed

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if self.event_observer is not None:
            self.event_observer(event_type, payload)

    def _execution_command(self, argv: Sequence[str]) -> list[str]:
        if self.policy.local_test_mode:
            return list(argv)

        command = [
            self.policy.docker_binary,
            "run",
            "--rm",
            "--network=none",
            f"--cpus={self.policy.cpus}",
            f"--memory={self.policy.memory}",
            f"--pids-limit={self.policy.pids_limit}",
            f"--user={_container_user()}",
            "--read-only",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
            f"--mount=type=bind,src={self.workspace.root},dst=/work",
        ]
        if self.workspace.challenge_files is not None:
            command.append(
                f"--mount=type=bind,src={self.workspace.challenge_files},dst=/challenge,readonly"
            )
        command.extend(["-w", "/work", self.policy.docker_image])
        command.extend(argv)
        return command

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
            **_aggregate_reports(reports),
        )


def command_fingerprint(argv: Sequence[str]) -> str:
    payload = json.dumps(list(argv), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _container_user() -> str:
    if os.name != "posix":
        return "10001:10001"
    uid = os.getuid()
    gid = os.getgid()
    if uid == 0:
        return "10001:10001"
    return f"{uid}:{gid}"


def _truncate(value: bytes, limit: int) -> bytes:
    if len(value) <= limit:
        return value
    suffix = f"\n[truncated to {limit} bytes]\n".encode()
    return value[: max(0, limit - len(suffix))] + suffix


def _findings_to_dict(findings: Iterable[Any]) -> dict[str, int]:
    output: dict[str, int] = {}
    for finding in findings:
        kind = str(finding.kind)
        output[kind] = output.get(kind, 0) + int(finding.count)
    return output


def _merge_findings(*finding_groups: Iterable[Any]) -> dict[str, int]:
    output: dict[str, int] = {}
    for findings in finding_groups:
        for kind, count in _findings_to_dict(findings).items():
            output[kind] = output.get(kind, 0) + count
    return output


def _aggregate_reports(reports: Sequence[WorkerReport]) -> dict[str, Any]:
    facts: list[str] = []
    fact_seen: set[str] = set()
    candidates: list[FlagCandidate] = []
    candidate_seen: set[str] = set()
    written_files: list[str] = []
    written_seen: set[str] = set()
    for report in reports:
        for fact in report.facts:
            if fact not in fact_seen:
                fact_seen.add(fact)
                facts.append(fact)
        for candidate in report.flag_candidates:
            if candidate.value not in candidate_seen:
                candidate_seen.add(candidate.value)
                candidates.append(candidate)
        if report.written_path and report.written_path not in written_seen:
            written_seen.add(report.written_path)
            written_files.append(report.written_path)
    return {
        "facts": facts,
        "flag_candidates": candidates,
        "written_files": written_files,
    }
