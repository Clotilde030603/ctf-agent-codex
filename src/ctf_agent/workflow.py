"""Integrated vertical slice for ingest, solve, verify, submit, and evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ctf_agent.auth_broker import AuthSessionBroker
from ctf_agent.budget import ModelBudgetBroker
from ctf_agent.capabilities import RuntimeCapabilitySnapshot, default_capability_provider
from ctf_agent.config import Settings
from ctf_agent.engine import Controller, RunContext, StateHandler, StateOutcome
from ctf_agent.evidence import TerminalRenderer
from ctf_agent.models.base import ModelBackend
from ctf_agent.models.factory import create_codex_backend
from ctf_agent.platforms.base import PlatformAdapter
from ctf_agent.schemas import Challenge, FlagCandidate, RunState, SpecialistResult
from ctf_agent.skills import SkillSelection
from ctf_agent.specialists.model import BackendFactory
from ctf_agent.state import StateStore, find_run_database
from ctf_agent.workflow_parts.artifacts import _promote_solver
from ctf_agent.workflow_parts.budget import _budget_report, _model_budget
from ctf_agent.workflow_parts.completion import reproduce as reproduce_handler
from ctf_agent.workflow_parts.completion import writeup as writeup_handler
from ctf_agent.workflow_parts.contexts import (
    _category_specialist,
    _planner_backend,
    _planning_context,
    _runtime_capability_snapshot,
    _skill_selection,
    _solver_context,
)
from ctf_agent.workflow_parts.evidence import evidence as evidence_handler
from ctf_agent.workflow_parts.planning import plan as plan_handler
from ctf_agent.workflow_parts.records import _candidate, _challenge, _specialist_results
from ctf_agent.workflow_parts.session import _adapter
from ctf_agent.workflow_parts.session import authenticate as authenticate_handler
from ctf_agent.workflow_parts.session import ingest as ingest_handler
from ctf_agent.workflow_parts.session import triage as triage_handler
from ctf_agent.workflow_parts.solving import solve as solve_handler
from ctf_agent.workflow_parts.submission import submit as submit_handler
from ctf_agent.workflow_parts.verification import verify as verify_handler


class AutonomousWorkflow:
    def __init__(
        self,
        settings: Settings,
        adapter: PlatformAdapter | None = None,
        *,
        planner_backend: ModelBackend | None = None,
        solver_backend_factory: BackendFactory = create_codex_backend,
        reviewer_backend_factory: BackendFactory = create_codex_backend,
        worker_local_test_mode: bool = False,
        worker_allowed_argv0: set[str] | None = None,
        terminal_renderer: TerminalRenderer | None = None,
    ) -> None:
        self.settings = settings
        self._adapter_override = adapter
        self._planner_backend_override = planner_backend
        self._solver_backend_factory = solver_backend_factory
        self._reviewer_backend_factory = reviewer_backend_factory
        self._worker_local_test_mode = worker_local_test_mode
        self._worker_allowed_argv0 = worker_allowed_argv0
        self._terminal_renderer = terminal_renderer or TerminalRenderer()
        self._auth_broker = AuthSessionBroker()
        self._capability_provider = default_capability_provider()
        self._runtime_capabilities: RuntimeCapabilitySnapshot | None = None
        self._resume_auth_required = False
        self._resume_overrides: dict[str, Any] = {}
        self.handlers: dict[RunState, StateHandler] = {
            RunState.AUTHENTICATE: self.authenticate,
            RunState.INGEST: self.ingest,
            RunState.TRIAGE: self.triage,
            RunState.PLAN: self.plan,
            RunState.SOLVE: self.solve,
            RunState.VERIFY: self.verify,
            RunState.SUBMIT: self.submit,
            RunState.EVIDENCE_PENDING: self.evidence,
            RunState.WRITEUP_PENDING: self.writeup,
            RunState.EVIDENCE: self.evidence,
            RunState.WRITEUP: self.writeup,
            RunState.REPRODUCE: self.reproduce,
        }

    @classmethod
    def from_run(
        cls,
        runs_dir: Path,
        run_id: str,
        *,
        overrides: dict[str, Any] | None = None,
    ) -> AutonomousWorkflow:
        database = find_run_database(runs_dir, run_id)
        snapshot = StateStore(database).load_settings_snapshot(run_id)
        settings = (
            snapshot.restore(runs_dir=runs_dir, overrides=overrides)
            if snapshot is not None
            else Settings.model_validate({"runs_dir": runs_dir, **(overrides or {})})
        )
        workflow = cls(settings)
        record = StateStore(database).load(run_id)
        workflow._resume_auth_required = record.state in {RunState.SOLVE, RunState.VERIFY}
        workflow._resume_overrides = dict(overrides or {})
        return workflow

    def controller(self) -> Controller:
        return Controller(self.settings, self.handlers, resume_overrides=self._resume_overrides)

    async def _adapter(self, context: RunContext) -> PlatformAdapter:
        return await _adapter(self, context)

    async def authenticate(self, context: RunContext) -> StateOutcome:
        return await authenticate_handler(self, context)

    async def ingest(self, context: RunContext) -> StateOutcome:
        return await ingest_handler(self, context)

    async def triage(self, context: RunContext) -> StateOutcome:
        return await triage_handler(self, context)

    async def plan(self, context: RunContext) -> StateOutcome:
        return await plan_handler(self, context)

    async def solve(self, context: RunContext) -> StateOutcome:
        return await solve_handler(self, context)

    async def verify(self, context: RunContext) -> StateOutcome:
        return await verify_handler(self, context)

    async def submit(self, context: RunContext) -> StateOutcome:
        return await submit_handler(self, context)

    async def evidence(self, context: RunContext) -> StateOutcome:
        return await evidence_handler(self, context)

    async def writeup(self, context: RunContext) -> StateOutcome:
        return await writeup_handler(self, context)

    async def reproduce(self, context: RunContext) -> StateOutcome:
        return await reproduce_handler(self, context)

    def _challenge(self, context: RunContext) -> Challenge:
        return _challenge(self, context)

    def _specialist_results(self, context: RunContext) -> list[SpecialistResult]:
        return _specialist_results(self, context)

    def _candidate(
        self,
        context: RunContext,
        *,
        allow_legacy_accepted: bool = False,
    ) -> FlagCandidate:
        return _candidate(self, context, allow_legacy_accepted=allow_legacy_accepted)

    @staticmethod
    def _load_json(path: Path) -> Any:
        import json

        return json.loads(path.read_text(encoding="utf-8"))

    def _model_budget(self, context: RunContext) -> ModelBudgetBroker:
        return _model_budget(self, context)

    def _budget_report(
        self, context: RunContext
    ) -> dict[str, int | str | dict[str, dict[str, int]]]:
        return _budget_report(self, context)

    def _planner_backend(self, context: RunContext, role: str) -> ModelBackend:
        return _planner_backend(self, context, role)

    def _planning_context(
        self, context: RunContext, triage_data: dict[str, Any]
    ) -> dict[str, object]:
        return _planning_context(self, context, triage_data)

    def _solver_context(self, context: RunContext, triage_data: object) -> dict[str, object]:
        return _solver_context(self, context, triage_data)

    def _skill_selection(self, context: RunContext, category: str) -> SkillSelection:
        return _skill_selection(self, context, category)

    _category_specialist = staticmethod(_category_specialist)

    def _runtime_capability_snapshot(
        self, run_dir: Path | None = None
    ) -> RuntimeCapabilitySnapshot:
        return _runtime_capability_snapshot(self, run_dir)

    _promote_solver = staticmethod(_promote_solver)

    @staticmethod
    def _challenge_url(context: RunContext) -> str:
        return str(context.values.get("challenge_url") or context.record.challenge_url)


__all__ = ["AutonomousWorkflow"]
