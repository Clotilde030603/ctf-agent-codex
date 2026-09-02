from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args], capture_output=True, text=True, cwd=Path(__file__).parents[1]
    )


def test_lane_inspect_corrupt_checkpoint_is_machine_readable(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE lane_checkpoints (lane_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
            "workspace_generation INTEGER NOT NULL, compatibility_fingerprint TEXT NOT NULL, "
            "payload_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO lane_checkpoints VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("lane-a", "run-a", 1, "fingerprint", "{not-json", "now", "now"),
        )
    result = _run("-m", "ctf_agent.lanes", "inspect", "--database", str(database), "--json")
    assert result.returncode == 1
    assert "CorruptLaneCheckpointError" in result.stderr
    assert "TypeError: super(type,obj)" not in result.stderr
    assert json.loads(result.stdout)["error"]["type"] == "CorruptLaneCheckpointError"


def test_projection_mandatory_overflow_fails_closed(tmp_path: Path) -> None:
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    input_path.write_text(json.dumps({"system": "s" * 8_000, "task": "task"}), encoding="utf-8")
    result = _run(
        "-m", "ctf_agent.context_projector", "--input", str(input_path),
        "--budget", "1", "--output", str(output_path),
    )
    assert result.returncode == 1
    assert "mandatory" in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""
    assert not output_path.exists()


def test_skills_without_category_returns_typer_usage_error() -> None:
    result = _run("-m", "ctf_agent.skills")
    assert result.returncode == 2
    assert "Missing option" in result.stderr
    assert "AttributeError" not in result.stderr


def test_solve_without_url_returns_typer_usage_error() -> None:
    result = _run("-m", "ctf_agent.cli", "solve")
    assert result.returncode == 2
    assert "Missing argument" in result.stderr
    assert "Traceback" not in result.stderr
