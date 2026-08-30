"""Runtime configuration with environment overrides and secret-safe defaults."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
DEFAULT_CTF_TOOL_IMAGE = "ctf-agent-codex-tools:0.1.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CTF_", env_file=".env", extra="ignore")

    runs_dir: Path = Path("runs")
    backend: Literal["codex", "static"] = "codex"
    planner_model: str = "gpt-5.6-sol"
    solver_model: str = "gpt-5.6-sol"
    verifier_model: str = "gpt-5.6-sol"
    planner_effort: ReasoningEffort = "high"
    solver_effort: ReasoningEffort = "xhigh"
    verifier_effort: ReasoningEffort = "high"
    model_timeout_seconds: float = Field(default=180, gt=0, le=1800)
    model_call_budget: int = Field(default=20, ge=1, le=200)
    max_model_context_bytes: int = Field(default=512 * 1024, ge=4096)
    max_workers: int = Field(default=3, ge=1, le=3)
    allow_static_fallback: bool = False
    total_run_timeout_seconds: float = Field(default=3600, gt=0, le=86400)
    worker_max_steps: int = Field(default=12, ge=1, le=100)
    worker_max_commands: int = Field(default=8, ge=0, le=100)
    worker_max_http_requests: int = Field(default=8, ge=0, le=100)
    worker_wall_time_seconds: float = Field(default=600, gt=0, le=3600)
    worker_no_progress_limit: int = Field(default=3, ge=1, le=20)
    request_timeout_seconds: float = Field(default=20, gt=0, le=120)
    tool_timeout_seconds: float = Field(default=30, gt=0, le=600)
    retry_budget: int = Field(default=2, ge=0, le=10)
    submission_budget: int = Field(default=1, ge=0, le=20)
    max_hypotheses: int = Field(default=3, ge=1, le=3)
    max_state_steps: int = Field(default=100, ge=10, le=1000)
    max_extraction_depth: int = Field(default=3, ge=0, le=10)
    max_extracted_bytes: int = Field(default=256 * 1024 * 1024, ge=1024)
    rate_limit_per_second: float = Field(default=2, gt=0, le=50)
    codex_binary: str = "codex"
    browser_storage_state: Path | None = None
    allow_private_hosts: bool = False
    allow_local_reproduction: bool = False
    approve_static_submission: bool = False
    redact_flag: bool = False
    docker_image: str = DEFAULT_CTF_TOOL_IMAGE


class RunSettingsSnapshot(BaseModel):
    """Versioned, credential-free settings persisted with a run."""

    schema_version: Literal[1] = 1
    backend: Literal["codex", "static"]
    planner_model: str
    solver_model: str
    verifier_model: str
    planner_effort: ReasoningEffort
    solver_effort: ReasoningEffort
    verifier_effort: ReasoningEffort
    model_timeout_seconds: float
    model_call_budget: int
    max_model_context_bytes: int
    max_workers: int
    allow_static_fallback: bool
    total_run_timeout_seconds: float
    worker_max_steps: int
    worker_max_commands: int
    worker_max_http_requests: int
    worker_wall_time_seconds: float
    worker_no_progress_limit: int
    request_timeout_seconds: float
    tool_timeout_seconds: float
    retry_budget: int
    submission_budget: int
    max_hypotheses: int
    max_state_steps: int
    max_extraction_depth: int
    max_extracted_bytes: int
    rate_limit_per_second: float
    codex_binary: str
    browser_storage_state: str | None
    allow_private_hosts: bool
    allow_local_reproduction: bool
    approve_static_submission: bool = False
    redact_flag: bool
    docker_image: str

    @classmethod
    def from_settings(cls, settings: Settings) -> RunSettingsSnapshot:
        payload = settings.model_dump(mode="json", exclude={"runs_dir"})
        return cls.model_validate(payload)

    def restore(
        self,
        *,
        runs_dir: Path,
        overrides: dict[str, Any] | None = None,
    ) -> Settings:
        payload = self.model_dump(exclude={"schema_version"})
        payload["runs_dir"] = runs_dir
        payload.update(overrides or {})
        return Settings.model_validate(payload)
