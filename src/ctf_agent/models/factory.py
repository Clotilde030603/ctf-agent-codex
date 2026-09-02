from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ctf_agent.config import Settings
from ctf_agent.events import EventLedger
from ctf_agent.models.base import ModelBackend
from ctf_agent.models.codex import CodexCliBackend

ProjectionEventObserver = Callable[[str, dict[str, Any]], None]


def create_codex_backend(
    settings: Settings,
    role: str,
    cwd: Path,
    *,
    projection_run_dir: Path | None = None,
) -> ModelBackend:
    model, reasoning_effort = _model_settings(settings, role)
    run_dir = projection_run_dir or _find_run_dir(cwd)
    artifact_root = (run_dir or cwd) / "artifacts" / "context"
    return CodexCliBackend(
        executable=settings.codex_binary,
        model=model,
        reasoning_effort=reasoning_effort,
        cwd=cwd,
        sandbox="read-only",
        timeout_seconds=settings.model_timeout_seconds,
        max_prompt_bytes=min(
            settings.max_model_context_bytes,
            _prompt_budget(settings, role),
        ),
        recent_report_limit=settings.context_recent_report_limit,
        projection_artifacts_dir=artifact_root,
        projection_event_observer=(
            _run_event_observer(run_dir) if run_dir is not None else None
        ),
    )


def _prompt_budget(settings: Settings, role: str) -> int:
    normalized = role.strip().lower()
    if normalized == "planner":
        return settings.planner_prompt_budget_bytes
    if normalized == "replan":
        return settings.replan_prompt_budget_bytes
    if normalized == "solver":
        return settings.solver_prompt_budget_bytes
    if normalized == "verifier":
        return settings.verifier_prompt_budget_bytes
    if normalized == "reviewer":
        return settings.reviewer_prompt_budget_bytes
    return settings.solver_prompt_budget_bytes


def _model_settings(settings: Settings, role: str) -> tuple[str, str]:
    normalized = role.strip().lower()
    if normalized in {"planner", "replan"}:
        return settings.planner_model, settings.planner_effort
    if normalized == "solver":
        return settings.solver_model, settings.solver_effort
    if normalized in {"verifier", "reviewer"}:
        return settings.verifier_model, settings.verifier_effort
    return settings.solver_model, settings.solver_effort


def _find_run_dir(cwd: Path) -> Path | None:
    resolved = cwd.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / "state.db").is_file():
            return candidate
    return None


def _run_event_observer(run_dir: Path) -> ProjectionEventObserver | None:
    database = run_dir / "state.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT run_id, state FROM runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    run_id, state = str(row[0]), str(row[1])
    ledger = EventLedger(database, run_dir / "events.jsonl")

    def observe(event_type: str, payload: dict[str, Any]) -> None:
        ledger.append(run_id, event_type, payload, state=state)

    return observe
