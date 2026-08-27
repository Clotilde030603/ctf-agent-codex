"""Deterministic workflow controller.

LLM backends may produce typed plans or solver results, but only this controller is
allowed to advance workflow state.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from ctf_agent.config import Settings
from ctf_agent.events import EventLedger
from ctf_agent.schemas import RunRecord, RunState
from ctf_agent.state import StateStore


@dataclass(slots=True)
class StateOutcome:
    target: RunState
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(slots=True)
class RunContext:
    record: RunRecord
    store: StateStore
    ledger: EventLedger
    settings: Settings
    values: dict[str, Any] = field(default_factory=dict)


StateHandler = Callable[[RunContext], Awaitable[StateOutcome]]


class Controller:
    def __init__(self, settings: Settings, handlers: dict[RunState, StateHandler]) -> None:
        self.settings = settings
        self.handlers = handlers

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
        for child in ("files", "artifacts", "evidence"):
            (run_dir / child).mkdir(parents=True, exist_ok=True)
        store = StateStore(run_dir / "state.db")
        record = RunRecord(
            run_id=run_id,
            challenge_url=url,
            run_dir=run_dir,
            auto_submit=auto_submit,
            writeup=writeup,
        )
        store.create(record)
        ledger = EventLedger(run_dir / "state.db", run_dir / "events.jsonl")
        ledger.append(
            run_id,
            "run.created",
            {"url": url, "auto_submit": auto_submit, "writeup": writeup},
            state=record.state.value,
            idempotency_key="run-created",
        )
        return RunContext(record, store, ledger, self.settings)

    def resume_run(self, run_id: str) -> RunContext:
        candidates = list(self.settings.runs_dir.glob(f"**/*{run_id}*/state.db"))
        if not candidates:
            candidates = list(self.settings.runs_dir.glob("**/state.db"))
        for database in candidates:
            store = StateStore(database)
            try:
                record = store.load(run_id)
            except KeyError:
                continue
            ledger = EventLedger(database, record.run_dir / "events.jsonl")
            ledger.append(
                run_id,
                "run.resumed",
                {"checkpoint": record.state.value},
                state=record.state.value,
            )
            return RunContext(record, store, ledger, self.settings)
        raise KeyError(f"run not found: {run_id}")

    async def execute(self, context: RunContext) -> RunRecord:
        while context.record.state not in {RunState.DONE, RunState.FAILED}:
            state = context.record.state
            handler = self.handlers.get(state)
            if handler is None:
                raise RuntimeError(f"no handler registered for {state}")
            task_key = f"state:{state.value}"
            context.ledger.append(
                context.record.run_id,
                "state.started",
                {},
                state=state.value,
            )
            try:
                outcome = await handler(context)
            except asyncio.CancelledError:
                context.ledger.append(
                    context.record.run_id,
                    "run.interrupted",
                    {"checkpoint": state.value},
                    state=state.value,
                )
                raise
            except Exception as exc:
                context.ledger.append(
                    context.record.run_id,
                    "state.error",
                    {"error_type": type(exc).__name__, "message": str(exc)},
                    state=state.value,
                )
                context.record = context.store.transition(
                    context.record.run_id, RunState.FAILED, str(exc)
                )
                return context.record
            context.store.checkpoint(context.record.run_id, task_key)
            context.ledger.append(
                context.record.run_id,
                "state.completed",
                outcome.payload,
                state=state.value,
                idempotency_key=task_key,
            )
            context.record = context.store.transition(
                context.record.run_id, outcome.target, outcome.error
            )
            context.ledger.append(
                context.record.run_id,
                "state.transition",
                {"from": state.value, "to": outcome.target.value},
                state=outcome.target.value,
            )
        return context.record
