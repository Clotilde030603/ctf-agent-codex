from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ctf_agent.auth_broker import AuthSessionHandle, AuthSessionStatus
from ctf_agent.config import Settings
from ctf_agent.engine import RunContext, StateOutcome
from ctf_agent.ingestion.session import ScopedAsyncSession, SessionConfig
from ctf_agent.schemas import AuthSession, RunState
from ctf_agent.scope import HostScope
from ctf_agent.workflow import AutonomousWorkflow
from ctf_agent.workflow_parts import session as session_workflow


class CredentialAdapter:
    def __init__(self, *, authenticated: bool, secret: str) -> None:
        self._authenticated = authenticated
        self.session = ScopedAsyncSession(
            HostScope.from_url("https://ctf.test"),
            config=SessionConfig(rate_limit_per_second=1000),
            client=httpx.AsyncClient(cookies={"session": secret}),
        )

    async def authenticate(self) -> AuthSession:
        return AuthSession(authenticated=self._authenticated)


def _checkpoint(context: RunContext, target: RunState) -> None:
    for state in (RunState.INGEST, RunState.TRIAGE, RunState.PLAN, RunState.SOLVE):
        context.record = context.store.transition(context.record.run_id, state)
    if target is RunState.VERIFY:
        context.record = context.store.transition(context.record.run_id, target)


@pytest.mark.asyncio
@pytest.mark.parametrize("resumed_state", [RunState.SOLVE, RunState.VERIFY])
async def test_restart_reauthenticates_then_returns_to_protected_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resumed_state: RunState,
) -> None:
    # Given: a protected-work checkpoint and a user-owned credential source.
    secret = "-".join(("restart", "cookie", "secret"))
    storage_state = tmp_path / "controller-storage.json"
    storage_state.write_text(
        json.dumps({"cookies": [{"name": "session", "value": secret}]}),
        encoding="utf-8",
    )
    settings = Settings(
        runs_dir=tmp_path / "runs",
        browser_storage_state=storage_state,
    )
    original = AutonomousWorkflow(settings)
    original_context = original.controller().create_run(
        "https://ctf.test/challenges/restart",
        auto_submit=False,
        writeup=False,
    )
    _checkpoint(original_context, resumed_state)
    adapter = CredentialAdapter(authenticated=True, secret=secret)

    async def detected(
        _url: str,
        **_kwargs: str | bool | Path | ScopedAsyncSession | None,
    ) -> CredentialAdapter:
        return adapter

    monkeypatch.setattr(session_workflow, "create_detected_adapter", detected)
    restarted = AutonomousWorkflow.from_run(settings.runs_dir, original_context.record.run_id)
    visited: list[RunState] = []
    acquired: list[AuthSessionHandle] = []

    async def protected_work(context: RunContext) -> StateOutcome:
        visited.append(context.record.state)
        handle = context.values.get("auth_handle")
        assert isinstance(handle, AuthSessionHandle)
        assert restarted._auth_broker.metadata(handle).status is AuthSessionStatus.AVAILABLE
        acquired.append(handle)
        return StateOutcome(RunState.FAILED, {"fixture_complete": True})

    restarted.handlers[resumed_state] = protected_work
    controller = restarted.controller()
    resumed = controller.resume_run(original_context.record.run_id)

    # When: a new process executes the resumed checkpoint.
    result = await controller.execute(resumed)
    await adapter.session.client.aclose()

    # Then: AUTHENTICATE reacquires an opaque handle before returning to prior work.
    transitions = [
        event["payload"]
        for event in resumed.ledger.list(result.run_id)
        if event["event_type"] == "state.transition"
    ]
    assert transitions[-3:] == [
        {"from": resumed_state.value, "to": RunState.AUTHENTICATE.value},
        {"from": RunState.AUTHENTICATE.value, "to": resumed_state.value},
        {"from": resumed_state.value, "to": RunState.FAILED.value},
    ]
    assert visited == [resumed_state]
    assert len(acquired) == 1
    assert secret not in repr(acquired[0])
    persisted = b"".join(
        path.read_bytes() for path in result.run_dir.rglob("*") if path.is_file()
    )
    assert secret.encode() not in persisted


@pytest.mark.asyncio
async def test_restart_without_credentials_needs_user_authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a protected checkpoint with no usable controller credential source.
    settings = Settings(runs_dir=tmp_path / "runs")
    original = AutonomousWorkflow(settings)
    original_context = original.controller().create_run(
        "https://ctf.test/challenges/no-credentials",
        auto_submit=False,
        writeup=False,
    )
    _checkpoint(original_context, RunState.SOLVE)
    adapter = CredentialAdapter(
        authenticated=False,
        secret="-".join(("unused", "secret")),
    )

    async def detected(
        _url: str,
        **_kwargs: str | bool | Path | ScopedAsyncSession | None,
    ) -> CredentialAdapter:
        return adapter

    monkeypatch.setattr(session_workflow, "create_detected_adapter", detected)
    restarted = AutonomousWorkflow.from_run(settings.runs_dir, original_context.record.run_id)

    async def protected_work(_context: RunContext) -> StateOutcome:
        raise AssertionError("protected work ran without authentication")

    restarted.handlers[RunState.SOLVE] = protected_work
    controller = restarted.controller()
    resumed = controller.resume_run(original_context.record.run_id)

    # When: the restarted process cannot authenticate.
    result = await controller.execute(resumed)
    await adapter.session.client.aclose()

    # Then: execution fails closed in an explicit user-action status, not a worker lane.
    assert result.state is RunState("NEEDS_AUTHENTICATION")
    assert result.last_error == "needs_authentication: user action required"
    assert not any(
        event["event_type"] == "solve.round"
        for event in resumed.ledger.list(result.run_id)
    )

    # Given: the user supplies credentials before retrying the durable action state.
    replacement = CredentialAdapter(
        authenticated=True,
        secret="-".join(("replacement", "secret")),
    )

    async def detected_replacement(
        _url: str,
        **_kwargs: str | bool | Path | ScopedAsyncSession | None,
    ) -> CredentialAdapter:
        return replacement

    monkeypatch.setattr(
        session_workflow, "create_detected_adapter", detected_replacement
    )
    retried_workflow = AutonomousWorkflow.from_run(
        settings.runs_dir, original_context.record.run_id
    )
    protected_work_resumed = False

    async def resumed_protected_work(_context: RunContext) -> StateOutcome:
        nonlocal protected_work_resumed
        protected_work_resumed = True
        return StateOutcome(RunState.FAILED)

    retried_workflow.handlers[RunState.SOLVE] = resumed_protected_work
    retried_controller = retried_workflow.controller()
    retried_context = retried_controller.resume_run(original_context.record.run_id)

    # When: the user retries the run with the credential source available.
    retried = await retried_controller.execute(retried_context)
    await replacement.session.client.aclose()

    # Then: the preserved intent returns to protected work rather than ingesting again.
    assert retried.state is RunState.FAILED
    assert protected_work_resumed is True
    retry_transitions = [
        event["payload"]
        for event in retried_context.ledger.list(retried.run_id)
        if event["event_type"] == "state.transition"
    ]
    assert retry_transitions[-3:] == [
        {
            "from": RunState.NEEDS_AUTHENTICATION.value,
            "to": RunState.AUTHENTICATE.value,
        },
        {"from": RunState.AUTHENTICATE.value, "to": RunState.SOLVE.value},
        {"from": RunState.SOLVE.value, "to": RunState.FAILED.value},
    ]
