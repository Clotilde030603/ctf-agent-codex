"""Runtime configuration with environment overrides and secret-safe defaults."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CTF_", env_file=".env", extra="ignore")

    runs_dir: Path = Path("runs")
    backend: Literal["codex", "static"] = "codex"
    planner_model: str = "gpt-5.6-sol"
    solver_model: str = "gpt-5.6-sol"
    verifier_model: str = "gpt-5.6-sol"
    planner_effort: str = "high"
    solver_effort: str = "xhigh"
    verifier_effort: str = "high"
    model_timeout_seconds: float = Field(default=180, gt=0, le=1800)
    model_call_budget: int = Field(default=20, ge=1, le=200)
    max_model_context_bytes: int = Field(default=512 * 1024, ge=4096)
    max_workers: int = Field(default=3, ge=1, le=3)
    allow_static_fallback: bool = True
    worker_max_steps: int = Field(default=12, ge=1, le=100)
    worker_max_commands: int = Field(default=8, ge=0, le=100)
    worker_wall_time_seconds: float = Field(default=600, gt=0, le=3600)
    worker_no_progress_limit: int = Field(default=3, ge=1, le=20)
    request_timeout_seconds: float = Field(default=20, gt=0, le=120)
    tool_timeout_seconds: float = Field(default=30, gt=0, le=600)
    retry_budget: int = Field(default=2, ge=0, le=10)
    submission_budget: int = Field(default=3, ge=0, le=20)
    max_hypotheses: int = Field(default=3, ge=1, le=3)
    max_state_steps: int = Field(default=100, ge=10, le=1000)
    max_extraction_depth: int = Field(default=3, ge=0, le=10)
    max_extracted_bytes: int = Field(default=256 * 1024 * 1024, ge=1024)
    rate_limit_per_second: float = Field(default=2, gt=0, le=50)
    codex_binary: str = "codex"
    browser_storage_state: Path | None = None
    allow_private_hosts: bool = False
    allow_local_reproduction: bool = False
    redact_flag: bool = False
    docker_image: str = "python:3.12-slim"
