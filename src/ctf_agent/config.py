"""Runtime configuration with environment overrides and secret-safe defaults."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
DEFAULT_CTF_TOOL_IMAGE = "ctf-agent-codex-tools:0.1.0"


@dataclass(frozen=True, slots=True)
class BudgetSettingsError(ValueError):
    field: str
    reason: str

    def __str__(self) -> str:
        return f"invalid {self.field}: {self.reason}"


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
    model_budget_hard_limit: int | None = Field(default=None, ge=1, le=400)
    model_budget_verifier_floor: int = Field(default=1, ge=0, le=20)
    model_budget_planner_soft_limit: int = Field(default=1, ge=0, le=20)
    model_budget_max_extensions: int = Field(default=0, ge=0, le=20)
    model_budget_extension_size: int = Field(default=1, ge=1, le=20)
    max_model_context_bytes: int = Field(default=196_608, ge=4096)
    planner_prompt_budget_bytes: int = Field(default=131_072, ge=4096)
    solver_prompt_budget_bytes: int = Field(default=196_608, ge=4096)
    verifier_prompt_budget_bytes: int = Field(default=131_072, ge=4096)
    reviewer_prompt_budget_bytes: int = Field(default=131_072, ge=4096)
    replan_prompt_budget_bytes: int = Field(default=131_072, ge=4096)
    context_recent_report_limit: int = Field(default=3, ge=0, le=100)
    max_workers: int = Field(default=3, ge=1, le=3)
    lane_quantum_steps: int = Field(default=2, ge=1, le=2)
    frontier_active_width: int = Field(default=3, ge=1, le=3)
    frontier_total_pool: int = Field(default=6, ge=1, le=12)
    frontier_max_rounds: int = Field(default=3, ge=2, le=12)
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
    max_hypotheses: int = Field(default=6, ge=1, le=12)
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
    tcp_controller_capability: Literal["unavailable"] = "unavailable"
    runtime_capability_mode: Literal["current", "corrected"] = "corrected"
    model_budget_mode: Literal["shared", "elastic"] = "elastic"
    lane_continuity_enabled: bool = True
    context_projection_enabled: bool = True
    adaptive_frontier_enabled: bool = True

    @model_validator(mode="after")
    def validate_model_budget(self) -> Settings:
        hard_limit = self.model_budget_hard_limit or self.model_call_budget
        if hard_limit < self.model_call_budget:
            raise BudgetSettingsError(
                "model_budget_hard_limit", "must not be below model_call_budget"
            )
        if self.model_budget_verifier_floor > self.model_call_budget:
            raise BudgetSettingsError(
                "model_budget_verifier_floor", "must fit within model_call_budget"
            )
        if self.model_budget_planner_soft_limit > self.model_call_budget:
            raise BudgetSettingsError(
                "model_budget_planner_soft_limit", "must fit within model_call_budget"
            )
        return self


class RunSettingsSnapshot(BaseModel):
    """Versioned, credential-free settings persisted with a run."""

    schema_version: Literal[1, 2] = 2
    backend: Literal["codex", "static"]
    planner_model: str
    solver_model: str
    verifier_model: str
    planner_effort: ReasoningEffort
    solver_effort: ReasoningEffort
    verifier_effort: ReasoningEffort
    model_timeout_seconds: float
    model_call_budget: int
    model_budget_hard_limit: int | None = None
    model_budget_verifier_floor: int = 1
    model_budget_planner_soft_limit: int = 1
    model_budget_max_extensions: int = 0
    model_budget_extension_size: int = 1
    max_model_context_bytes: int
    planner_prompt_budget_bytes: int = 131_072
    solver_prompt_budget_bytes: int = 196_608
    verifier_prompt_budget_bytes: int = 131_072
    reviewer_prompt_budget_bytes: int = 131_072
    replan_prompt_budget_bytes: int = 131_072
    context_recent_report_limit: int = 3
    max_workers: int
    lane_quantum_steps: int = 2
    frontier_active_width: int = 3
    frontier_total_pool: int = 6
    frontier_max_rounds: int = 3
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
    tcp_controller_capability: Literal["unavailable"] = "unavailable"
    runtime_capability_mode: Literal["current", "corrected"] = "corrected"
    model_budget_mode: Literal["shared", "elastic"] = "elastic"
    lane_continuity_enabled: bool = True
    context_projection_enabled: bool = True
    adaptive_frontier_enabled: bool = True

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
