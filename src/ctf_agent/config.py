"""Runtime configuration with environment overrides and secret-safe defaults."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CTF_", env_file=".env", extra="ignore")

    runs_dir: Path = Path("runs")
    planner_model: str = "gpt-5.6-sol"
    solver_model: str = "gpt-5.6-sol"
    verifier_model: str = "gpt-5.6-sol"
    planner_effort: str = "high"
    solver_effort: str = "xhigh"
    verifier_effort: str = "high"
    request_timeout_seconds: float = Field(default=20, gt=0, le=120)
    tool_timeout_seconds: float = Field(default=30, gt=0, le=600)
    retry_budget: int = Field(default=2, ge=0, le=10)
    submission_budget: int = Field(default=3, ge=0, le=20)
    max_hypotheses: int = Field(default=3, ge=1, le=3)
    max_extraction_depth: int = Field(default=3, ge=0, le=10)
    max_extracted_bytes: int = Field(default=256 * 1024 * 1024, ge=1024)
    rate_limit_per_second: float = Field(default=2, gt=0, le=50)
    codex_binary: str = "codex"
    browser_storage_state: Path | None = None
    allow_private_hosts: bool = False
    docker_image: str = "python:3.12-slim"
