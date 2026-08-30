import asyncio
from pathlib import Path

import pytest

from ctf_agent.config import Settings
from ctf_agent.engine import Controller, RunContext, StateOutcome
from ctf_agent.schemas import RunState
from ctf_agent.workflow import AutonomousWorkflow


@pytest.mark.asyncio
async def test_controller_owns_all_state_transitions_and_resumes(tmp_path: Path) -> None:
    states = [
        RunState.AUTHENTICATE,
        RunState.INGEST,
        RunState.TRIAGE,
        RunState.PLAN,
        RunState.SOLVE,
        RunState.VERIFY,
        RunState.SUBMIT,
        RunState.EVIDENCE,
        RunState.WRITEUP,
        RunState.REPRODUCE,
    ]
    targets = states[1:] + [RunState.DONE]

    async def make_outcome(context: RunContext) -> StateOutcome:
        index = states.index(context.record.state)
        return StateOutcome(targets[index], {"visited": context.record.state.value})

    controller = Controller(
        Settings(runs_dir=tmp_path / "runs"), {state: make_outcome for state in states}
    )
    context = controller.create_run(
        "https://ctf.test/challenges/7", auto_submit=True, writeup=True
    )
    result = await controller.execute(context)
    assert result.state is RunState.DONE

    resumed = controller.resume_run(result.run_id)
    assert resumed.record.state is RunState.DONE
    transitions = [
        event
        for event in resumed.ledger.list(result.run_id)
        if event["event_type"] == "state.transition"
    ]
    assert len(transitions) == 10


def test_sensitive_url_query_is_redacted_from_state_and_events(tmp_path: Path) -> None:
    async def unused_handler(context: RunContext) -> StateOutcome:
        return StateOutcome(RunState.INGEST)

    controller = Controller(
        Settings(runs_dir=tmp_path / "runs"),
        {RunState.AUTHENTICATE: unused_handler},
    )
    context = controller.create_run(
        "https://ctf.test/challenges/7?token=secret-value&view=full",
        auto_submit=False,
        writeup=False,
    )

    assert "secret-value" not in context.record.challenge_url
    assert "token=REDACTED" in context.record.challenge_url
    assert "secret-value" not in (context.record.run_dir / "events.jsonl").read_text()
    with pytest.raises(RuntimeError, match="credential-bearing"):
        controller.resume_run(context.record.run_id)

    resumed = controller.resume_run(
        context.record.run_id,
        challenge_url="https://ctf.test/challenges/7?token=secret-value&view=full",
    )
    assert resumed.values["challenge_url"].endswith("token=secret-value&view=full")


@pytest.mark.asyncio
async def test_controller_enforces_total_run_timeout(tmp_path: Path) -> None:
    async def slow_handler(context: RunContext) -> StateOutcome:
        await asyncio.sleep(0.1)
        return StateOutcome(RunState.INGEST)

    controller = Controller(
        Settings(
            runs_dir=tmp_path / "runs",
            total_run_timeout_seconds=0.02,
        ),
        {RunState.AUTHENTICATE: slow_handler},
    )
    context = controller.create_run(
        "https://ctf.test/challenges/slow", auto_submit=False, writeup=False
    )

    result = await controller.execute(context)

    assert result.state is RunState.FAILED
    assert result.last_error == "total run timeout exhausted"
    assert any(
        event["event_type"] == "run.timeout"
        for event in context.ledger.list(result.run_id)
    )


@pytest.mark.asyncio
async def test_resume_does_not_replay_state_if_event_append_crashes_after_commit(
    tmp_path: Path,
) -> None:
    calls = 0

    async def authenticate(context: RunContext) -> StateOutcome:
        nonlocal calls
        calls += 1
        return StateOutcome(RunState.INGEST)

    controller = Controller(
        Settings(runs_dir=tmp_path / "runs"),
        {RunState.AUTHENTICATE: authenticate},
    )
    context = controller.create_run(
        "https://ctf.test/challenges/crash", auto_submit=False, writeup=False
    )
    original_append = context.ledger.append

    def crashing_append(*args: object, **kwargs: object) -> int:
        if len(args) > 1 and args[1] == "state.completed":
            raise RuntimeError("simulated event append crash")
        return original_append(*args, **kwargs)

    context.ledger.append = crashing_append  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="simulated event append crash"):
        await controller.execute(context)

    resumed = controller.resume_run(context.record.run_id)
    assert resumed.record.state is RunState.INGEST
    assert calls == 1


def test_workflow_resume_restores_snapshot_and_only_explicit_overrides(tmp_path: Path) -> None:
    original = Settings(
        runs_dir=tmp_path / "runs",
        backend="codex",
        planner_model="planner-a",
        solver_model="solver-a",
        verifier_model="reviewer-a",
        planner_effort="low",
        solver_effort="xhigh",
        verifier_effort="medium",
        model_call_budget=31,
        worker_max_commands=13,
        allow_private_hosts=True,
        redact_flag=True,
    )
    workflow = AutonomousWorkflow(original)
    context = workflow.controller().create_run(
        "https://ctf.test/challenges/settings?token=do-not-store",
        auto_submit=False,
        writeup=True,
    )

    restored = AutonomousWorkflow.from_run(
        original.runs_dir,
        context.record.run_id,
        overrides={"solver_model": "solver-b", "solver_effort": "ultra"},
    )

    assert restored.settings.planner_model == "planner-a"
    assert restored.settings.solver_model == "solver-b"
    assert restored.settings.verifier_model == "reviewer-a"
    assert restored.settings.planner_effort == "low"
    assert restored.settings.solver_effort == "ultra"
    assert restored.settings.model_call_budget == 31
    assert restored.settings.worker_max_commands == 13
    assert restored.settings.allow_private_hosts is True
    assert restored.settings.redact_flag is True
    database_text = (context.record.run_dir / "state.db").read_bytes()
    assert b"do-not-store" not in database_text

    resumed = restored.controller().resume_run(
        context.record.run_id,
        challenge_url="https://ctf.test/challenges/settings?token=do-not-store",
    )
    resume_event = [
        event
        for event in resumed.ledger.list(context.record.run_id)
        if event["event_type"] == "run.resumed"
    ][-1]
    overrides = resume_event["payload"]["settings"]["overrides"]
    assert set(overrides) == {"solver_effort", "solver_model"}


def test_invalid_settings_snapshot_fails_loudly(tmp_path: Path) -> None:
    settings = Settings(runs_dir=tmp_path / "runs")
    workflow = AutonomousWorkflow(settings)
    context = workflow.controller().create_run(
        "https://ctf.test/challenges/invalid-settings",
        auto_submit=False,
        writeup=False,
    )
    with context.store._connect() as connection:
        connection.execute(
            "UPDATE run_settings SET payload_json=? WHERE run_id=?",
            ('{"schema_version":1,"backend":"invalid"}', context.record.run_id),
        )

    with pytest.raises(RuntimeError, match="invalid persisted run settings snapshot"):
        AutonomousWorkflow.from_run(settings.runs_dir, context.record.run_id)
