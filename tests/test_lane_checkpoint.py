from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from ctf_agent.config import Settings
from ctf_agent.events import EventLedger
from ctf_agent.lanes import (
    CorruptLaneCheckpointError,
    LaneCheckpoint,
    LaneCheckpointConflictError,
    LaneCheckpointStore,
    LaneModelIdentity,
    ProvenancedFact,
    stable_lane_id,
)
from ctf_agent.models.base import ModelRequest, ModelResponse
from ctf_agent.schemas import Hypothesis, RunRecord
from ctf_agent.specialists.model import ModelSolverSpecialist
from ctf_agent.state import StateStore
from ctf_agent.workers import CommandPolicy, LaneWorkspace, WorkerBudget, WorkerCore


class ContinuityBackend:
    def __init__(self, decisions: list[dict[str, Any]]) -> None:
        self.decisions = list(decisions)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(content=json.dumps(self.decisions.pop(0)))


def _hypothesis() -> Hypothesis:
    return Hypothesis(
        id="H-continuity",
        claim="follow a long deterministic chain",
        supporting_evidence=["twelve dependent observations are required"],
        expected_signal="a replayed candidate after step ten",
        cost="high",
        confidence=0.7,
        required_tools=["python"],
        kill_condition="a chain link is falsified",
        success_condition="solve.py reproduces the candidate",
    )


def test_worker_records_controller_provenance_and_keeps_finish_claim_untrusted(
    tmp_path: Path,
) -> None:
    # Given: one fact attached to controller-observed output and one model-only claim.
    worker = WorkerCore(
        ContinuityBackend(
            [
                {
                    "action": "run",
                    "argv": [sys.executable, "-c", "print('observed')"],
                    "facts": ["command produced observed output"],
                },
                {"action": "finish", "facts": ["unsupported model claim"]},
            ]
        ),
        LaneWorkspace(tmp_path / "lane"),
        budget=WorkerBudget(max_steps=2, max_commands=1),
        policy=CommandPolicy(
            allowed_argv0={Path(sys.executable).name}, local_test_mode=True
        ),
    )

    # When: the imported worker executes and checkpoints both actions.
    outcome = asyncio.run(
        worker.run_slice("observe", checkpoint=_checkpoint(), max_steps=2)
    )

    # Then: only controller-observed evidence validates a typed fact.
    observed, model_only = outcome.checkpoint.facts
    assert isinstance(observed, ProvenancedFact)
    assert observed.fact == "command produced observed output"
    assert observed.source == "command"
    assert observed.artifact is not None and observed.artifact.endswith(".stdout.txt")
    assert observed.command == (sys.executable, "-c", "print('observed')")
    assert len(observed.evidence_sha256) == 64
    assert observed.status == "validated"
    assert observed.sequence == 1
    assert model_only.fact == "unsupported model claim"
    assert model_only.source == "model"
    assert model_only.artifact is None
    assert model_only.command == ()
    assert model_only.status == "untrusted"
    assert model_only.sequence == 2


def test_hypothesis_revision_preserves_compatible_lane_continuity(tmp_path: Path) -> None:
    # Given: durable controller-validated progress under one runtime identity.
    store = LaneCheckpointStore(tmp_path / "state.db")
    fact = ProvenancedFact(
        fact="ELF header observed",
        source="artifact",
        artifact="files/challenge.bin",
        command=("file", "files/challenge.bin"),
        evidence_sha256="a" * 64,
        status="validated",
        sequence=3,
    )
    current = store.save(
        _checkpoint().model_copy(update={"step_index": 3, "facts": (fact,)})
    )
    revised = _checkpoint().model_copy(
        update={
            "hypothesis_revision": "revision-2",
            "hypothesis": "decode the revised payload",
            "restatement": "decode revised payload deterministically",
        }
    )

    # When: only the hypothesis revision changes.
    resumed, reset = store.resume_or_reset(revised)

    # Then: compatible progress survives without a new workspace generation.
    assert reset is False
    assert resumed.hypothesis_revision == "revision-2"
    assert resumed.workspace_generation == current.workspace_generation
    assert resumed.step_index == 3
    assert resumed.facts == (fact,)


def test_checkpoint_lifecycle_events_are_sanitized_and_explicit(tmp_path: Path) -> None:
    # Given: the imported store writes through the imported durable event ledger.
    database = tmp_path / "state.db"
    ledger = EventLedger(database, tmp_path / "events.jsonl")

    def observe(event_type: str, payload: dict[str, Any]) -> None:
        ledger.append("run-a", event_type, payload)

    store = LaneCheckpointStore(database, event_observer=observe)

    # When: a lane is created, resumed, updated, and reset for runtime incompatibility.
    created, _ = store.resume_or_reset(_checkpoint())
    store.resume_or_reset(_checkpoint())
    store.save(created.model_copy(update={"next_action": "token=supersecret"}))
    changed_identity = created.model_identity.model_copy(update={"model": "solver-v2"})
    store.resume_or_reset(_checkpoint(identity=changed_identity))

    # Then: ordered lifecycle events carry explicit continuity metadata and no secret.
    events = ledger.list("run-a")
    assert [event["event_type"] for event in events] == [
        "lane.checkpoint.created",
        "lane.checkpoint.resumed",
        "lane.checkpoint.updated",
        "lane.checkpoint.reset",
    ]
    assert [event["payload"]["reason"] for event in events] == [
        "initial_checkpoint",
        "compatible_runtime_identity",
        "checkpoint_saved",
        "runtime_identity_incompatible",
    ]
    for event in events:
        assert {
            "lane_id",
            "reason",
            "revision",
            "workspace_generation",
        } <= event["payload"].keys()
    assert "supersecret" not in (tmp_path / "events.jsonl").read_text()


def test_worker_core_exposes_bounded_run_slice_checkpoint_seam() -> None:
    # Given: the imported worker execution object used by existing specialists.
    # When: bounded execution support is inspected on that object.
    run_slice = getattr(WorkerCore, "run_slice", None)

    # Then: it must be a callable public seam rather than another monolithic run.
    assert callable(run_slice)


def test_model_slice_checkpoint_continuity_survives_reopen_after_step_two(
    tmp_path: Path,
) -> None:
    # Given: a lane whose successful solver requires more than ten worker actions.
    run_dir = tmp_path / "run"
    (run_dir / "files").mkdir(parents=True)
    database = run_dir / "state.db"
    state = StateStore(database)
    state.create(
        RunRecord(
            run_id="run-continuity",
            challenge_url="https://ctf.test/challenges/continuity",
            run_dir=run_dir,
        )
    )
    candidate = {
        "value": "flag{after_twelve_steps}",
        "source_artifact": "solve.py",
        "source_location": "stdout line 1",
        "derivation": ["long chain"],
        "solver_command": f"{sys.executable} solve.py",
        "confidence": 0.9,
    }
    decisions: list[dict[str, Any]] = [
        {
            "action": "write_file",
            "path": f"notes/step-{step}.txt",
            "content": f"observation {step}\n",
            "facts": [f"verified fact {step}"],
            "message": f"continue with step {step + 1}",
        }
        for step in range(1, 11)
    ] + [
        {
            "action": "write_file",
            "path": "solve.py",
            "content": "print('flag{after_twelve_steps}')\n",
            "facts": ["solver assembled from ten verified facts"],
        },
        {
            "action": "run",
            "argv": [sys.executable, "solve.py"],
            "facts": ["solver replay succeeded"],
            "flag_candidates": [candidate],
        },
        {"action": "finish", "message": "candidate reproduced", "flag_candidates": [candidate]},
    ]
    backend = ContinuityBackend(decisions)
    settings = Settings(
        backend="codex",
        runs_dir=tmp_path / "runs",
        worker_max_steps=20,
        worker_max_commands=3,
        worker_no_progress_limit=3,
    )
    context: dict[str, object] = {
        "run_id": "run-continuity",
        "run_dir": str(run_dir),
        "state_database": str(database),
        "classification": {"primary_category": "crypto-binary"},
        "challenge": {"title": "Continuity", "attachment_urls": []},
        "runtime_capabilities": {},
    }
    first_specialist = ModelSolverSpecialist(
        settings,
        backend_factory=lambda _settings, _role, _cwd: backend,
        local_test_mode=True,
        allowed_argv0={Path(sys.executable).name},
    )
    run_slice = getattr(first_specialist, "run_slice", None)
    assert callable(run_slice)

    # When: exactly two actions run, the process-facing objects are recreated, and slices resume.
    first = asyncio.run(run_slice(_hypothesis(), context, max_steps=2))
    assert first.checkpoint.step_index == 2
    reopened_state = StateStore(database)
    checkpoints = getattr(reopened_state, "lane_checkpoints", None)
    assert callable(checkpoints)
    durable = checkpoints().load(first.checkpoint.lane_id)
    assert durable is not None
    assert durable.step_index == 2
    assert durable.verified_facts == ("verified fact 1", "verified fact 2")
    assert durable.next_action == "continue with step 3"

    resumed_specialist = ModelSolverSpecialist(
        settings,
        backend_factory=lambda _settings, _role, _cwd: backend,
        local_test_mode=True,
        allowed_argv0={Path(sys.executable).name},
    )
    outcome = first
    while outcome.status == "progress":
        outcome = asyncio.run(
            resumed_specialist.run_slice(_hypothesis(), context, max_steps=2)
        )

    # Then: durable continuity reaches the post-step-ten success without restarting at step one.
    assert outcome.status == "solved"
    assert outcome.checkpoint.step_index == 13
    assert outcome.specialist_result.status == "confirmed"
    assert [item.value for item in outcome.specialist_result.flag_candidates] == [
        "flag{after_twelve_steps}"
    ]
    assert len(backend.requests) == 13


def _checkpoint(
    lane_id: str = "lane-a",
    *,
    identity: LaneModelIdentity | None = None,
) -> LaneCheckpoint:
    return LaneCheckpoint(
        lane_id=lane_id,
        run_id="run-a",
        hypothesis_id="H1",
        hypothesis_revision="revision-1",
        category="crypto-binary",
        model_identity=identity
        or LaneModelIdentity(
            specialist="model-solver",
            model="solver-v1",
            effort="high",
            skill_sha256="skill-v1",
            capability_sha256="capability-v1",
            attachment_sha256="attachment-v1",
        ),
        hypothesis="decode payload",
        restatement="decode payload deterministically",
    )


def test_checkpoint_restart_restores_facts_failed_approaches_and_next_action(
    tmp_path: Path,
) -> None:
    # Given: a committed machine-consumed continuation checkpoint.
    database = tmp_path / "state.db"
    store = StateStore(database).lane_checkpoints()
    expected = _checkpoint().model_copy(
        update={
            "step_index": 4,
            "verified_facts": ("header is XOR encoded",),
            "failed_approaches": ("plain base64 decode failed",),
            "next_action": "derive the repeating XOR key",
        }
    )
    store.save(expected)

    # When: all process-local objects are discarded and the database is reopened.
    restored = StateStore(database).lane_checkpoints().load(expected.lane_id)

    # Then: continuation state comes entirely from SQLite.
    assert restored is not None
    assert restored.verified_facts == expected.verified_facts
    assert restored.failed_approaches == expected.failed_approaches
    assert restored.next_action == expected.next_action
    assert restored.step_index == 4


def test_corrupt_checkpoint_fails_closed_until_explicit_reset(tmp_path: Path) -> None:
    # Given: a valid row whose serialized checkpoint is corrupted externally.
    database = tmp_path / "state.db"
    store = StateStore(database).lane_checkpoints()
    seed = _checkpoint()
    store.save(seed)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE lane_checkpoints SET payload_json=? WHERE lane_id=?",
            ("{not-json", seed.lane_id),
        )

    # When/Then: loading fails closed and identifies the required recovery operation.
    with pytest.raises(CorruptLaneCheckpointError, match="explicit reset required"):
        StateStore(database).lane_checkpoints().load(seed.lane_id)

    reset = StateStore(database).lane_checkpoints().reset(seed.lane_id, seed)
    assert reset.workspace_generation == 2
    assert StateStore(database).lane_checkpoints().load(seed.lane_id) == reset


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("model", "solver-v2"),
        ("skill_sha256", "skill-v2"),
        ("capability_sha256", "capability-v2"),
        ("attachment_sha256", "attachment-v2"),
    ],
)
def test_model_identity_change_resets_workspace_generation(
    tmp_path: Path,
    field: str,
    changed: str,
) -> None:
    # Given: durable progress under one complete execution identity.
    store = StateStore(tmp_path / "state.db").lane_checkpoints()
    seed = _checkpoint().model_copy(
        update={"step_index": 7, "verified_facts": ("old fact",)}
    )
    store.save(seed)
    changed_identity = seed.model_identity.model_copy(update={field: changed})

    # When: model, skill, capability, or attachment identity changes.
    replacement, reset = store.resume_or_reset(
        _checkpoint(identity=changed_identity)
    )

    # Then: the stable lane gets an explicit clean generation.
    assert reset is True
    assert replacement.lane_id == seed.lane_id
    assert replacement.workspace_generation == 2
    assert replacement.step_index == 0
    assert replacement.verified_facts == ()


def test_two_lane_ids_remain_isolated(tmp_path: Path) -> None:
    # Given: two lanes for distinct hypotheses in one run database.
    store = StateStore(tmp_path / "state.db").lane_checkpoints()
    first = _checkpoint("lane-a").model_copy(update={"verified_facts": ("fact-a",)})
    second = _checkpoint("lane-b").model_copy(
        update={"hypothesis_id": "H2", "verified_facts": ("fact-b",)}
    )

    # When: both checkpoints are committed independently.
    store.save(first)
    store.save(second)

    # Then: each primary key retains only its own continuation state.
    loaded_first = store.load("lane-a")
    loaded_second = store.load("lane-b")
    assert loaded_first is not None
    assert loaded_second is not None
    assert loaded_first.verified_facts == ("fact-a",)
    assert loaded_second.verified_facts == ("fact-b",)
    assert {item.lane_id for item in store.list("run-a")} == {"lane-a", "lane-b"}


def test_concurrent_stale_checkpoint_writer_is_rejected(tmp_path: Path) -> None:
    # Given: two independent writers loaded the same committed checkpoint revision.
    database = tmp_path / "state.db"
    store = StateStore(database).lane_checkpoints()
    store.save(_checkpoint())
    first = store.load("lane-a")
    second = StateStore(database).lane_checkpoints().load("lane-a")
    assert first is not None
    assert second is not None
    barrier = threading.Barrier(2)

    def save(checkpoint: LaneCheckpoint, fact: str) -> LaneCheckpoint | Exception:
        barrier.wait()
        try:
            return StateStore(database).lane_checkpoints().save(
                checkpoint.model_copy(update={"verified_facts": (fact,)})
            )
        except LaneCheckpointConflictError as exc:
            return exc

    # When: both stale snapshots attempt to advance concurrently.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(save, (first, second), ("first", "second")))

    # Then: exactly one compare-and-swap commits and the stale writer is rejected.
    assert sum(isinstance(item, LaneCheckpoint) for item in outcomes) == 1
    assert sum(isinstance(item, LaneCheckpointConflictError) for item in outcomes) == 1
    durable = store.load("lane-a")
    assert durable is not None
    assert durable.revision == 2


def test_secret_bearing_action_text_and_candidate_never_persist(tmp_path: Path) -> None:
    # Given: a model decision containing credential text and a raw flag candidate.
    database = tmp_path / "state.db"
    candidate = {
        "value": "flag{must_remain_ephemeral}",
        "source_artifact": "stdout",
        "source_location": "line 1",
        "solver_command": "python3 solve.py",
    }
    worker = WorkerCore(
        ContinuityBackend(
            [
                {
                    "action": "finish",
                    "message": "api_key=supersecret",
                    "facts": ["token=supersecret"],
                    "flag_candidates": [candidate],
                }
            ]
        ),
        LaneWorkspace(tmp_path / "lane"),
    )

    # When: the slice checkpoint is committed.
    outcome = asyncio.run(
        worker.run_slice("finish", checkpoint=_checkpoint(), max_steps=1)
    )
    StateStore(database).lane_checkpoints().save(outcome.checkpoint)
    persisted = database.read_bytes()

    # Then: only redacted text and a one-way candidate identity are durable.
    assert b"supersecret" not in persisted
    assert b"flag{must_remain_ephemeral}" not in persisted
    assert outcome.checkpoint.next_action == "api_key=[REDACTED]"
    assert outcome.checkpoint.verified_facts == ("token=[REDACTED]",)
    assert len(outcome.checkpoint.candidate_history) == 1


def test_schema_migration_preserves_compatible_run_state(tmp_path: Path) -> None:
    # Given: a version-four database from before lane checkpoint support.
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY,challenge_url TEXT NOT NULL,"
            "run_dir TEXT NOT NULL,state TEXT NOT NULL,auto_submit INTEGER NOT NULL,"
            "writeup INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,"
            "last_error TEXT)"
        )
        connection.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "legacy-v4",
                "https://ctf.test/c/legacy",
                str(tmp_path),
                "AUTHENTICATE",
                0,
                0,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                None,
            ),
        )
        connection.execute("PRAGMA user_version=4")

    # When: the current state store opens and migrates it.
    migrated = StateStore(database)

    # Then: compatible state survives and lane storage is available.
    assert migrated.load("legacy-v4").run_id == "legacy-v4"
    assert migrated.lane_checkpoints().list() == ()
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7


def test_modified_solve_py_gets_new_fingerprint_while_identical_is_deduped(
    tmp_path: Path,
) -> None:
    # Given: one lane generation that writes and executes a solver repeatedly.
    workspace = LaneWorkspace(tmp_path / "lane")
    backend = ContinuityBackend(
        [
            {"action": "write_file", "path": "solve.py", "content": "print('v1')\n"},
            {"action": "run", "argv": [sys.executable, "solve.py"]},
            {"action": "run", "argv": [sys.executable, "solve.py"]},
            {"action": "run", "argv": [sys.executable, "solve.py"]},
        ]
    )
    worker = WorkerCore(
        backend,
        workspace,
        budget=WorkerBudget(max_steps=8, max_commands=3),
        policy=CommandPolicy(
            allowed_argv0={Path(sys.executable).name}, local_test_mode=True
        ),
    )
    checkpoint = _checkpoint()
    checkpoint = asyncio.run(
        worker.run_slice("write", checkpoint=checkpoint, max_steps=1)
    ).checkpoint
    first_run = asyncio.run(
        worker.run_slice("run", checkpoint=checkpoint, max_steps=1)
    )
    duplicate = asyncio.run(
        worker.run_slice("run", checkpoint=first_run.checkpoint, max_steps=1)
    )

    # When: solve.py changes without changing the lane generation.
    workspace.write_relative_file("solve.py", "print('v2')\n")
    modified = asyncio.run(
        worker.run_slice("run", checkpoint=duplicate.checkpoint, max_steps=1)
    )

    # Then: identical content is skipped, while modified content executes under a new hash.
    assert first_run.result.reports[0].status == "ok"
    assert duplicate.result.reports[0].status == "skipped"
    assert modified.result.reports[0].status == "ok"
    assert (
        first_run.result.reports[0].command_fingerprint
        != modified.result.reports[0].command_fingerprint
    )
    assert modified.checkpoint.workspace_generation == checkpoint.workspace_generation


def test_stable_lane_id_depends_on_run_hypothesis_and_specialist() -> None:
    assert stable_lane_id("run-a", "H1", "model") == stable_lane_id(
        "run-a", "H1", "model"
    )
    assert stable_lane_id("run-a", "H1", "model") != stable_lane_id(
        "run-a", "H2", "model"
    )
