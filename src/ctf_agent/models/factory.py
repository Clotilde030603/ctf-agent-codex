from __future__ import annotations

from pathlib import Path

from ctf_agent.config import Settings
from ctf_agent.models.base import ModelBackend
from ctf_agent.models.codex import CodexCliBackend, SandboxMode


def create_codex_backend(settings: Settings, role: str, cwd: Path) -> ModelBackend:
    model, reasoning_effort = _model_settings(settings, role)
    sandbox: SandboxMode = (
        "workspace-write" if role.strip().lower() == "solver" else "read-only"
    )
    return CodexCliBackend(
        executable=settings.codex_binary,
        model=model,
        reasoning_effort=reasoning_effort,
        cwd=cwd,
        sandbox=sandbox,
        timeout_seconds=settings.model_timeout_seconds,
        max_prompt_bytes=settings.max_model_context_bytes,
    )


def _model_settings(settings: Settings, role: str) -> tuple[str, str]:
    normalized = role.strip().lower()
    if normalized == "planner":
        return settings.planner_model, settings.planner_effort
    if normalized == "solver":
        return settings.solver_model, settings.solver_effort
    if normalized == "verifier":
        return settings.verifier_model, settings.verifier_effort
    return settings.solver_model, settings.solver_effort
