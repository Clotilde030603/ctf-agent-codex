"""Deterministic workflow controller.

LLM backends may produce typed plans or solver results, but only this controller is
allowed to advance workflow state.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from ctf_agent.budget_types import BudgetPolicy
from ctf_agent.config import RunSettingsSnapshot, Settings
from ctf_agent.engine_transitions import execute_transitions
from ctf_agent.engine_types import RunContext, StateHandler, StateOutcome
from ctf_agent.events import EventLedger
from ctf_agent.schemas import RunRecord, RunState
from ctf_agent.security import protect_directory, redact_url
from ctf_agent.state import StateStore


def _model_budget_policy(settings: Settings) -> BudgetPolicy:
    return BudgetPolicy(
        initial_limit=settings.model_call_budget,
        hard_limit=settings.model_budget_hard_limit or settings.model_call_budget,
        verifier_floor=(
            settings.model_budget_verifier_floor
            if settings.backend == "codex" and settings.model_budget_mode == "elastic"
            else 0
        ),
        planner_soft_limit=(
            settings.model_budget_planner_soft_limit
            if settings.model_budget_mode == "elastic"
            else 0
        ),
        max_extensions=(
            settings.model_budget_max_extensions
            if settings.model_budget_mode == "elastic"
            else 0
        ),
        extension_size=settings.model_budget_extension_size,
    )


class Controller:
    def __init__(
        self,
        settings: Settings,
        handlers: dict[RunState, StateHandler],
        *,
        resume_overrides: dict[str, Any] | None = None,
    ) -> None:
        self.settings = settings
        self.handlers = handlers
        self.resume_overrides = dict(resume_overrides or {})

    @staticmethod
    def _run_id(url: str) -> str:
        return f"{hashlib.sha256(url.encode()).hexdigest()[:10]}-{uuid4().hex[:8]}"

    @staticmethod
    def _run_directory(runs_dir: Path, url: str, run_id: str) -> Path:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        event = re.sub(r"[^a-zA-Z0-9_.-]+", "-", parsed.hostname or "unknown").strip("-")
        challenge = re.sub(r"[^a-zA-Z0-9_.-]+", "-", parsed.path.strip("/") or "challenge")
        return runs_dir / event / f"{challenge}-{run_id}"

    def create_run(self, url: str, *, auto_submit: bool, writeup: bool) -> RunContext:
        run_id = self._run_id(url)
        run_dir = self._run_directory(self.settings.runs_dir, url, run_id).resolve()
        protect_directory(run_dir)
        for child in ("files", "artifacts", "evidence"):
            protect_directory(run_dir / child)
        store = StateStore(run_dir / "state.db")
        record = RunRecord(
            run_id=run_id,
            challenge_url=redact_url(url),
            run_dir=run_dir,
            auto_submit=auto_submit,
            writeup=writeup,
        )
        store.create(record, RunSettingsSnapshot.from_settings(self.settings))
        model_budget = store.model_budget_broker(
            run_id,
            _model_budget_policy(self.settings),
        )
        ledger = EventLedger(run_dir / "state.db", run_dir / "events.jsonl")
        ledger.append(
            run_id,
            "run.created",
            {"url": url, "auto_submit": auto_submit, "writeup": writeup},
            state=record.state.value,
            idempotency_key="run-created",
        )
        return RunContext(
            record,
            store,
            ledger,
            self.settings,
            values={"challenge_url": url, "model_budget": model_budget},
        )

    def resume_run(self, run_id: str, *, challenge_url: str | None = None) -> RunContext:
        candidates = list(self.settings.runs_dir.glob(f"**/*{run_id}*/state.db"))
        if not candidates:
            candidates = list(self.settings.runs_dir.glob("**/state.db"))
        for database in candidates:
            store = StateStore(database)
            try:
                record = store.load(run_id)
            except KeyError:
                continue
            snapshot = store.load_settings_snapshot(run_id)
            if snapshot is None:
                settings_payload: dict[str, Any] = {
                    "snapshot": "missing",
                    "migration": "safe current defaults",
                }
            else:
                restored = snapshot.restore(
                    runs_dir=self.settings.runs_dir,
                    overrides=self.resume_overrides,
                )
                for key, value in restored.model_dump().items():
                    setattr(self.settings, key, value)
                active_values = restored.model_dump(mode="json")
                stored_values = snapshot.restore(
                    runs_dir=self.settings.runs_dir
                ).model_dump(mode="json")
                changed = {
                    key: {"stored": stored, "active": active_values.get(key)}
                    for key, stored in stored_values.items()
                    if key != "runs_dir" and active_values.get(key) != stored
                }
                settings_payload = {
                    "snapshot_schema_version": snapshot.schema_version,
                    "overrides": changed,
                }
            if challenge_url is None and "REDACTED" in record.challenge_url:
                raise RuntimeError(
                    "this run used a credential-bearing challenge URL; pass the original "
                    "URL with resume --challenge-url so it remains memory-only"
                )
            runtime_url = challenge_url or record.challenge_url
            if redact_url(runtime_url) != record.challenge_url:
                raise ValueError("resume challenge URL does not match the stored run")
            model_budget = store.model_budget_broker(
                run_id,
                _model_budget_policy(self.settings),
            )
            ledger = EventLedger(database, record.run_dir / "events.jsonl")
            model_budget.reconcile_events(ledger.list(run_id))
            ledger.append(
                run_id,
                "run.resumed",
                {"checkpoint": record.state.value, "settings": settings_payload},
                state=record.state.value,
            )
            return RunContext(
                record,
                store,
                ledger,
                self.settings,
                values={
                    "challenge_url": runtime_url,
                    "model_budget": model_budget,
                    "resumed": True,
                },
            )
        raise KeyError(f"run not found: {run_id}")

    def retry_evidence(
        self, run_id: str, *, challenge_url: str | None = None
    ) -> RunContext:
        context = self.resume_run(run_id, challenge_url=challenge_url)
        context.record = context.store.prepare_evidence_retry(run_id)
        context.ledger.append(
            run_id,
            "evidence.retry_requested",
            {"accepted_verdict_preserved": True},
            state=RunState.EVIDENCE_PENDING.value,
        )
        return context

    async def execute(self, context: RunContext) -> RunRecord:
        return await execute_transitions(context, self.handlers, self.settings)


__all__ = ["Controller", "RunContext", "StateHandler", "StateOutcome"]
