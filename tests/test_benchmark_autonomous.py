from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from ctf_agent.benchmark import BenchmarkManifest, benchmark, solve_at_k
from ctf_agent.benchmark_runner import AutonomousArtifactError, score_autonomous_artifacts

type ManifestValue = (
    bool | str | int | None | list[str] | dict[str, bool | str | int | None]
)

_SOLVE_SOURCE = (
    "from pathlib import Path\n"
    "print('flag{' + Path('input.txt').read_text(encoding='utf-8').strip() + '}')\n"
)


def _write_autonomous_fixture(root: Path) -> None:
    (root / "input.txt").write_text("autonomous_ok\n", encoding="utf-8")
    (root / "run.py").write_text(
        """import hashlib
import json
import sqlite3
from pathlib import Path

secret = Path("input.txt").read_text(encoding="utf-8").strip()
candidate = "flag{" + secret + "}"
solve_source = (
    "from pathlib import Path\\n"
    "print('flag{' + Path('input.txt').read_text(encoding='utf-8').strip() + '}')\\n"
)
Path("solve.py").write_text(solve_source, encoding="utf-8")
solver_hash = hashlib.sha256(solve_source.encode()).hexdigest()
candidate_json = json.dumps({
    "value": candidate,
    "source_artifact": "input.txt",
    "source_location": "input.txt:1",
    "derivation": ["read fixture input"],
    "solver_command": "python3 solve.py",
    "format_match": True,
    "provenance_verified": True,
    "replay_verified": True,
    "data_dependency_verified": True,
    "independent_verified": True,
    "submission_allowed": True,
    "confidence": 1.0,
})
with sqlite3.connect("run.sqlite3") as connection:
    connection.execute("CREATE TABLE runs (run_id TEXT, state TEXT)")
    connection.execute("INSERT INTO runs VALUES (?, ?)", ("pilot-run", "DONE"))
    connection.execute(
        "CREATE TABLE verified_candidates ("
        "run_id TEXT, candidate_json TEXT, solver_sha256 TEXT, "
        "source_artifact TEXT, source_artifact_sha256 TEXT, verified_at TEXT, "
        "valid INTEGER, invalidation_reason TEXT)"
    )
    connection.execute(
        "INSERT INTO verified_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "pilot-run",
            candidate_json,
            solver_hash,
            "input.txt",
            hashlib.sha256(Path("input.txt").read_bytes()).hexdigest(),
            "2026-01-01T00:00:00+00:00",
            1,
            None,
        ),
    )
events = [
    {"run_id": "pilot-run", "type": "model.request", "seconds": 1.0, "payload": {}},
    {"run_id": "pilot-run", "type": "worker.command", "seconds": 2.0, "payload": {}},
    {
        "run_id": "pilot-run",
        "type": "flag.candidate",
        "seconds": 3.0,
        "payload": {"hallucinated": False},
    },
    {
        "run_id": "pilot-run",
        "type": "flag.verified",
        "seconds": 4.0,
        "payload": {
            "accepted": True,
            "replay_verified": True,
            "data_dependency_verified": True,
            "independent_verified": True,
        },
    },
    {
        "run_id": "pilot-run",
        "type": "usage.scored",
        "payload": {"model_cost": 999, "tool_cost": 999, "network_cost": 999},
    },
    {"run_id": "pilot-run", "type": "state.transition", "payload": {"to": "DONE"}},
]
Path("events.jsonl").write_text(
    "".join(json.dumps(event) + "\\n" for event in events), encoding="utf-8"
)
Path("benchmark-metrics.json").write_text(
    json.dumps({"model_calls": 999, "wrong_submissions": 999}), encoding="utf-8"
)
Path("artifacts").mkdir()
Path("artifacts/runtime-capabilities.json").write_text(
    json.dumps({"image_digest": "sha256:forged"}), encoding="utf-8"
)
Path("artifacts/runtime-skills.json").write_text(
    json.dumps({"selected_skills": [{"skill_id": "forged"}]}), encoding="utf-8"
)
print(candidate)
""",
        encoding="utf-8",
    )
    (root / "benchmark-metrics.json").write_text(
        json.dumps({"model_calls": 999, "wrong_submissions": 999}),
        encoding="utf-8",
    )
    (root / "self-reported-events.jsonl").write_text(
        json.dumps(
            {
                "type": "usage.scored",
                "payload": {"model_cost": 999, "tool_cost": 999},
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_autonomous_metrics_are_scorer_derived_and_ignore_self_reporting(
    tmp_path: Path,
) -> None:
    _write_autonomous_fixture(tmp_path)
    manifest = tmp_path / "manifest.v2.yaml"
    manifest.write_text(
        """schema_version: 2
repeat_runs: 1
challenges:
  - id: autonomous-metrics
    runner: autonomous_workflow
    category: misc
    difficulty: {label: null, source: unknown, source_value: null}
    availability: local
    source: self-authored
    license: MIT
    authorized_for_benchmark: true
    redistribution:
      allowed: true
      evidence_url: https://example.invalid/repository-license
    contamination:
      status: controlled
      details: synthetic pilot fixture
    command: [python3, run.py]
    expected_flag: flag{autonomous_ok}
    artifact_paths: [input.txt]
    flag_policy: {pattern: 'flag\\{[^{}]+\\}'}
    metrics_source: self_reported
    events_file: self-reported-events.jsonl
""",
        encoding="utf-8",
    )

    result = benchmark(manifest)

    challenge = result["challenges"][0]
    run = challenge["runs"][0]
    assert challenge["execution_group"] == "autonomous_workflow"
    assert challenge["model_calls"] > 1
    assert challenge["wrong_submissions"] == 0
    assert run["metrics"]["model_cost"] == run["metrics"]["model_calls"]
    assert run["metrics"]["tool_cost"] == run["metrics"]["tool_calls"] / 2
    assert run["metrics"]["network_cost"] == 0.0
    assert run["self_reported_metrics"]["model_cost"] == 999
    assert run["authoritative_metrics_source"] == "scorer_invocation"
    assert len(run["run_identity"]["run_id"]) == 19
    assert run["run_identity"]["run_id"] != "pilot-run"
    assert run["run_identity"]["tool_image_digest"] == "ctf-agent-codex-tools:0.1.0"
    assert run["verified_candidate"] is True
    assert run["final_state"] == "READY"
    assert len(run["promoted_solver_sha256"]) == 64
    assert run["promoted_solver_sha256"] != hashlib.sha256(_SOLVE_SOURCE.encode()).hexdigest()
    assert run["command"] is None
    assert run["clean_replay_success"] is True
    assert result["solve_at_1"] == 1.0
    assert result["solve_at_3"] == 1.0


def test_command_owned_autonomous_artifacts_are_not_an_authority(
    tmp_path: Path,
) -> None:
    # Given: an evaluated command manufactures every artifact the legacy scorer trusts.
    _write_autonomous_fixture(tmp_path)
    subprocess.run(
        [sys.executable, "run.py"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    # When / Then: direct scoring without a scorer-created invocation is rejected.
    with pytest.raises(AutonomousArtifactError, match="scorer-owned invocation"):
        score_autonomous_artifacts(tmp_path)


@pytest.mark.parametrize(
    "challenge_update",
    [
        {"authorized_for_benchmark": False},
        {"redistribution": {"allowed": True}},
        {"difficulty": "retired"},
    ],
)
def test_v2_metadata_rejects_untrusted_or_invalid_provenance(
    challenge_update: dict[str, ManifestValue],
) -> None:
    challenge: dict[str, ManifestValue] = {
        "id": "metadata",
        "runner": "fixture_command",
        "command": ["python3", "solve.py"],
        "expected_flag": "flag{x}",
        "authorized_for_benchmark": True,
        "difficulty": {"label": None, "source": "unknown", "source_value": None},
        "availability": "retired",
        "redistribution": {
            "allowed": True,
            "evidence_url": "https://example.invalid/license",
        },
        "contamination": {"status": "unknown"},
    }
    challenge.update(challenge_update)

    with pytest.raises(ValidationError):
        BenchmarkManifest.model_validate({"schema_version": 2, "challenges": [challenge]})


def test_solve_at_k_uses_any_verified_solve_in_first_k_attempts() -> None:
    assert solve_at_k([False, False, True], 1) is False
    assert solve_at_k([False, False, True], 3) is True
    assert solve_at_k([True], 3) is True
