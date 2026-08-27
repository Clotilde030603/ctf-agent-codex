from pathlib import Path

import pytest

from ctf_agent.config import Settings
from ctf_agent.engine import Controller, RunContext, StateOutcome
from ctf_agent.schemas import RunState


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
