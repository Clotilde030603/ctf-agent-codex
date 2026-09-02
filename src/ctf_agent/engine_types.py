"""Controller state-handler contracts and mutable run context."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

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
