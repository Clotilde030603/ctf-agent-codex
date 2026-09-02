"""Typed decisions, reports, and results for the controlled worker loop."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ctf_agent.lanes.model import LaneCheckpoint, LaneStatus
from ctf_agent.schemas import FlagCandidate

WorkerAction = Literal["run", "write_file", "http_request", "tcp_connect", "finish"]
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
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    tcp_payload: str | None = None
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
        tcp_fields_present = (
            self.host is not None
            or self.port is not None
            or self.tcp_payload is not None
        )
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
                or tcp_fields_present
            ):
                raise ValueError("run action may not include unrelated fields")
        elif self.action == "write_file":
            if not self.path:
                raise ValueError("write_file action requires path")
            if self.content is None:
                raise ValueError("write_file action requires content")
            if self.argv or request_fields_present or tcp_fields_present:
                raise ValueError("write_file action may not include unrelated fields")
        elif self.action == "http_request":
            if self.method is None or self.url is None:
                raise ValueError("http_request action requires method and url")
            if self.argv or self.path is not None or self.content is not None or tcp_fields_present:
                raise ValueError("http_request may not include argv/path/content/TCP fields")
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
        elif self.action == "tcp_connect":
            if self.host is None or self.port is None:
                raise ValueError("tcp_connect action requires host and port")
            if (
                self.argv
                or self.path is not None
                or self.content is not None
                or request_fields_present
            ):
                raise ValueError("tcp_connect may not include unrelated fields")
        elif self.action == "finish":
            if (
                self.argv
                or self.path is not None
                or self.content is not None
                or request_fields_present
                or tcp_fields_present
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


class WorkerReport(BaseModel):
    step: int
    action: WorkerAction
    status: Literal["ok", "skipped", "failed", "timeout"]
    message: str = ""
    argv: list[str] = Field(default_factory=list)
    command: list[str] = Field(default_factory=list)
    command_fingerprint: str | None = None
    output_fingerprint: str | None = None
    execution_receipt: str | None = None
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


class WorkerSliceResult(BaseModel):
    status: LaneStatus
    checkpoint: LaneCheckpoint
    result: WorkerResult


class WorkerExecutionError(RuntimeError):
    pass
