from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

import pytest

from ctf_agent.capabilities import RuntimeCapabilitySnapshot
from ctf_agent.config import Settings
from ctf_agent.engine_transitions import execute_transitions
from ctf_agent.engine_types import RunContext, StateOutcome
from ctf_agent.events import EventLedger
from ctf_agent.schemas import FlagCandidate, RunRecord, RunState, VerifiedCandidateRecord
from ctf_agent.skills import SkillRuntime, SkillSelection, ToolRouting
from ctf_agent.state import StateStore
from ctf_agent.writeup.generator import WriteupGenerator

_BEARER_FIXTURE = "durable-bearer-secret-901"
_API_FIXTURE = "durable-api-secret-902"
_SECRET_BYTES = (_BEARER_FIXTURE.encode(), _API_FIXTURE.encode())


class PersistenceFixtureError(RuntimeError):
    """Synthetic handler failure containing credential-shaped values."""


def _assert_private_and_clean(paths: tuple[Path, ...]) -> None:
    for path in paths:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        content = path.read_bytes()
        for secret in _SECRET_BYTES:
            assert secret not in content


def _sqlite_bytes(database: Path) -> bytes:
    with sqlite3.connect(database) as connection:
        values = connection.execute(
            "SELECT group_concat(value, '\n') FROM ("
            "SELECT challenge_url AS value FROM runs UNION ALL "
            "SELECT COALESCE(last_error, '') FROM runs UNION ALL "
            "SELECT candidate_json FROM verified_candidates UNION ALL "
            "SELECT COALESCE(invalidation_reason, '') FROM verified_candidates UNION ALL "
            "SELECT value FROM rejected_candidates UNION ALL "
            "SELECT reason FROM rejected_candidates UNION ALL "
            "SELECT payload FROM events)"
        ).fetchone()[0]
    return str(values).encode()


@pytest.mark.asyncio
async def test_durable_repositories_and_engine_events_redact_secrets(tmp_path: Path) -> None:
    # Given: every repository-facing field contains credentials in a machine-valid record.
    database = tmp_path / "private" / "state.db"
    ledger_path = tmp_path / "private" / "events.jsonl"
    store = StateStore(database)
    record = RunRecord(
        run_id="durable-security",
        challenge_url=f"https://ctf.test/c/1?api_key={_API_FIXTURE}&view=safe",
        run_dir=tmp_path / "private",
        last_error=f"Authorization: Bearer {_BEARER_FIXTURE}",
    )
    store.create(record)
    verified = VerifiedCandidateRecord(
        run_id=record.run_id,
        candidate=FlagCandidate(
            value="flag{safe_value}",
            source_artifact="files/input.txt",
            source_location=f"api_key={_API_FIXTURE}",
            derivation=[f"Authorization: Bearer {_BEARER_FIXTURE}"],
            solver_command="python3 solve.py",
        ),
        solver_sha256="a" * 64,
        source_artifact=f"files/proof?api_key={_API_FIXTURE}",
        source_artifact_sha256="b" * 64,
        invalidation_reason=f"Authorization: Bearer {_BEARER_FIXTURE}",
    )
    store.save_verified_candidate(verified)
    store.reject_candidate(
        record.run_id,
        "flag{rejected_safe_value}",
        f"api_key={_API_FIXTURE}",
    )
    ledger = EventLedger(database, ledger_path)
    context = RunContext(
        record=store.load(record.run_id),
        store=store,
        ledger=ledger,
        settings=Settings(),
    )

    async def fail(_context: RunContext) -> StateOutcome:
        raise PersistenceFixtureError(
            f"Authorization: Bearer {_BEARER_FIXTURE}; api_key={_API_FIXTURE}"
        )

    # When: the engine persists the handler failure and transition event.
    await execute_transitions(context, {RunState.AUTHENTICATE: fail}, Settings())

    # Then: schemas and safe values survive, but neither SQLite nor JSONL contains a secret.
    restored = store.load_verified_candidate(record.run_id)
    assert restored is not None
    assert restored.candidate.value == "flag{safe_value}"
    assert "view=safe" in store.load(record.run_id).challenge_url
    persisted = _sqlite_bytes(database) + ledger_path.read_bytes()
    for secret in _SECRET_BYTES:
        assert secret not in persisted
    _assert_private_and_clean((database, ledger_path))
    assert stat.S_IMODE(database.parent.stat().st_mode) == 0o700


def test_runtime_budget_and_writeup_artifacts_are_private_and_redacted(tmp_path: Path) -> None:
    # Given: durable artifact models and writeup inputs include both credential forms.
    artifacts = tmp_path / "artifacts"
    skills_path = artifacts / "runtime-skills.json"
    capabilities_path = artifacts / "runtime-capabilities.json"
    budget_path = artifacts / "model-budget.json"
    routing = ToolRouting(
        category=f"api_key={_API_FIXTURE}",
        planner_skill_ids=(),
        solver_skill_ids=(),
        verifier_skill_ids=(),
        allowed_actions=("run",),
    )
    SkillSelection(
        skills=(),
        runtime=SkillRuntime(identities=(), tool_routing=routing),
        developer_instructions="safe",
    ).write(skills_path)
    RuntimeCapabilitySnapshot(
        docker_image="safe-image:1",
        image_digest=None,
        capabilities=(),
        probe_reason=f"Authorization: Bearer {_BEARER_FIXTURE}",
    ).write(capabilities_path)
    from ctf_agent.workflow_parts.io import _write_json

    _write_json(
        budget_path,
        {"schema_version": 1, "active_limit": 2, "note": f"api_key={_API_FIXTURE}"},
    )
    (tmp_path / "challenge.json").write_text(
        json.dumps({
            "title": "Safe title",
            "description": f"Authorization: Bearer {_BEARER_FIXTURE}",
            "api_key": _API_FIXTURE,
        }),
        encoding="utf-8",
    )

    # When: public writeup outputs and provenance are generated.
    outputs = WriteupGenerator().generate_all(tmp_path, redact_flags=True)

    # Then: all named artifacts preserve JSON shape, redact credentials, and are owner-only.
    paths = (
        skills_path,
        capabilities_path,
        budget_path,
        outputs.markdown_path,
        outputs.html_path,
        outputs.provenance_path,
    )
    _assert_private_and_clean(paths)
    assert json.loads(skills_path.read_text())["schema_version"] == 1
    assert json.loads(capabilities_path.read_text())["docker_image"] == "safe-image:1"
    assert json.loads(budget_path.read_text())["active_limit"] == 2
    assert stat.S_IMODE(artifacts.stat().st_mode) == 0o700
