from pathlib import Path

import pytest

from ctf_agent.config import RunSettingsSnapshot, Settings
from ctf_agent.events import EventLedger
from ctf_agent.schemas import RunRecord, RunState
from ctf_agent.state import InvalidTransition, StateStore


def test_state_transition_and_resume(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    store = StateStore(database)
    record = RunRecord(run_id="r1", challenge_url="https://ctf.test/challenges/1", run_dir=tmp_path)
    store.create(record)
    store.transition("r1", RunState.INGEST)

    resumed = StateStore(database).load("r1")
    assert resumed.state is RunState.INGEST
    with pytest.raises(InvalidTransition):
        store.transition("r1", RunState.SUBMIT)


def test_settings_snapshot_round_trip_and_schema_migration(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    store = StateStore(database)
    settings = Settings(
        runs_dir=tmp_path / "runs",
        solver_model="solver-custom",
        solver_effort="ultra",
        model_call_budget=37,
        allow_private_hosts=True,
    )
    record = RunRecord(
        run_id="settings-run",
        challenge_url="https://ctf.test/challenges/1",
        run_dir=tmp_path,
    )
    store.create(record, RunSettingsSnapshot.from_settings(settings))

    restored = store.load_settings_snapshot(record.run_id)

    assert restored is not None
    active = restored.restore(runs_dir=settings.runs_dir)
    assert active.solver_model == "solver-custom"
    assert active.solver_effort == "ultra"
    assert active.model_call_budget == 37
    assert active.allow_private_hosts is True
    with store._connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] >= 2


def test_legacy_database_without_snapshot_remains_loadable(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create(
        RunRecord(
            run_id="legacy",
            challenge_url="https://ctf.test/challenges/legacy",
            run_dir=tmp_path,
        )
    )

    assert StateStore(tmp_path / "state.db").load("legacy").run_id == "legacy"
    assert store.load_settings_snapshot("legacy") is None


def test_checkpoint_is_idempotent(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create(RunRecord(run_id="r1", challenge_url="https://ctf.test/c/1", run_dir=tmp_path))
    store.checkpoint("r1", "ingest", tmp_path / "challenge.json")
    store.checkpoint("r1", "ingest", tmp_path / "challenge.json")
    assert store.is_complete("r1", "ingest")


def test_state_completion_atomically_checkpoints_and_transitions(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create(
        RunRecord(run_id="r1", challenge_url="https://ctf.test/c/1", run_dir=tmp_path)
    )

    record = store.complete_state(
        "r1",
        expected_state=RunState.AUTHENTICATE,
        target=RunState.INGEST,
        task_key="state:AUTHENTICATE",
    )

    assert record.state is RunState.INGEST
    assert store.is_complete("r1", "state:AUTHENTICATE")


def test_event_ledger_deduplicates_idempotency_key(tmp_path: Path) -> None:
    ledger = EventLedger(tmp_path / "state.db", tmp_path / "events.jsonl")
    first = ledger.append("r1", "http", {"url": "https://ctf.test"}, idempotency_key="req-1")
    second = ledger.append("r1", "http", {"url": "https://ctf.test"}, idempotency_key="req-1")
    assert first == second
    assert len(ledger.list("r1")) == 1
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == 1


def test_abandoned_auth_submission_does_not_consume_budget(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.create(
        RunRecord(run_id="r1", challenge_url="https://ctf.test/c/1", run_dir=tmp_path)
    )
    store.begin_submission("r1", "flag{x}", "attempt-1")
    assert store.submission_count("r1") == 1

    store.abandon_submission("attempt-1", "auth_required")

    assert store.submission_count("r1") == 0
    assert store.pending_submission("r1") is None
