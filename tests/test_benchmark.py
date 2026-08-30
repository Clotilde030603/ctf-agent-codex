from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path

import pytest

from ctf_agent.benchmark import _derive_event_metrics, benchmark


def test_yaml_benchmark_manifest_repeats_in_fresh_copies(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("benchmark_ok\n", encoding="utf-8")
    solver = tmp_path / "solve.py"
    solver.write_text(
        """
from pathlib import Path

counter = Path("counter.txt")
print(f"fresh={not counter.exists()}")
counter.write_text("seen", encoding="utf-8")
secret = Path("secret.txt").read_text(encoding="utf-8").strip()
print(f"flag{{{secret}}}")
""",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """challenges:
  - id: retired-warmup
    command: [python3, solve.py]
    expected_flag: flag{benchmark_ok}
""",
        encoding="utf-8",
    )

    result = benchmark(manifest)

    assert result["solved_count"] == 1
    assert result["run_count"] == 3
    assert result["clean_reproduction_rate"] == 1
    for run in result["challenges"][0]["runs"]:
        assert "fresh=True" in run["command"]["stdout"]


def test_benchmark_timeout_is_recorded(tmp_path: Path) -> None:
    solver = tmp_path / "solve.py"
    solver.write_text("import time\ntime.sleep(2)\n", encoding="utf-8")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """repeat_runs: 1
timeout_seconds: 0.1
challenges:
  - id: timeout
    command: [python3, solve.py]
    expected_flag: flag{never}
    clean_replay: false
""",
        encoding="utf-8",
    )

    result = benchmark(manifest)

    run = result["challenges"][0]["runs"][0]
    assert result["solved_count"] == 0
    assert run["timed_out"] is True
    assert run["command"]["exit_code"] == 124
    assert run["fixture_command_success"] is False


def test_benchmark_collects_declared_and_event_metrics(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("metric_ok\n", encoding="utf-8")
    metrics_payload = {
        "wrong_submissions": 1,
        "model_calls": 2,
        "tool_calls": 3,
        "hallucinated_candidates": 1,
        "candidate_count": 4,
        "time_to_candidate_seconds": 9.0,
        "events": [
            {"type": "flag.submitted", "seconds": 1.0, "payload": {"verdict": "wrong"}},
            {"type": "model.request"},
            {"type": "worker.command"},
            {"type": "flag.candidate", "seconds": 2.5, "payload": {"hallucinated": True}},
            {
                "type": "flag.submitted",
                "seconds": 3.5,
                "payload": {"verdict": "accepted"},
            },
            {"type": "flag.verified", "payload": {"accepted": True}},
            {"type": "writeup.validated", "payload": {"ok": True}},
            {"type": "run.resumed"},
        ],
    }
    solver = tmp_path / "solve.py"
    solver.write_text(
        f"""
import json
from pathlib import Path

metrics_json = {json.dumps(json.dumps(metrics_payload))}
Path("benchmark-metrics.json").write_text(metrics_json, encoding="utf-8")
secret = Path("secret.txt").read_text(encoding="utf-8").strip()
print("flag{{" + secret + "}}")
""",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """repeat_runs: 1
challenges:
  - id: metrics
    command: [python3, solve.py]
    expected_flag: flag{metric_ok}
    metrics_source: self_reported
""",
        encoding="utf-8",
    )

    result = benchmark(manifest)

    challenge = result["challenges"][0]
    assert challenge["wrong_submissions"] == 0
    assert challenge["model_calls"] == 0
    assert challenge["tool_calls"] == 2
    assert challenge["hallucinated_candidate_rate"] is None
    assert challenge["time_to_candidate_seconds"] > 0
    assert challenge["time_to_accepted_seconds"] is None
    assert result["replay_verified_rate"] == 1
    assert result["independent_verified_rate"] is None
    assert result["writeup_validated_rate"] is None
    assert result["resume_verified_rate"] is None
    self_reported = challenge["runs"][0]["self_reported_metrics"]
    assert self_reported["wrong_submissions"] == 1
    assert self_reported["model_calls"] == 1
    assert self_reported["tool_calls"] == 1
    assert self_reported["hallucinated_candidates"] == 1
    assert self_reported["candidate_count"] == 1
    assert self_reported["replay_verified"] is None


def test_event_metrics_require_explicit_success_and_completed_resume() -> None:
    missing = _derive_event_metrics(
        [
            {"type": "solver.replayed", "payload": {}},
            {"type": "independent.verified", "payload": {}},
            {"type": "writeup.validated", "payload": {}},
            {"type": "run.resumed", "payload": {}},
        ]
    )

    assert missing["replay_verified"] is None
    assert missing["independent_verified"] is None
    assert missing["writeup_validated"] is None
    assert missing["resume_verified"] is None

    explicit = _derive_event_metrics(
        [
            {"type": "run.resumed", "payload": {}},
            {"type": "model.request", "payload": {}},
            {"type": "worker.command", "payload": {"accepted": True}},
            {"type": "worker.http_request", "payload": {"accepted": True}},
            {"type": "flag.candidate", "seconds": 1.0, "payload": {}},
            {"type": "flag.rejected", "payload": {}},
            {
                "type": "flag.verified",
                "seconds": 2.0,
                "payload": {
                    "accepted": True,
                    "replay_verified": True,
                    "data_dependency_verified": True,
                    "independent_verified": True,
                },
            },
            {"type": "evidence.captured", "payload": {"accepted": True}},
            {"type": "writeup.validated", "payload": {"accepted": True}},
            {"type": "state.transition", "payload": {"to": "DONE"}},
        ]
    )

    assert explicit["model_calls"] == 1
    assert explicit["tool_calls"] == 2
    assert explicit["candidate_count"] == 1
    assert explicit["rejected_candidates"] == 1
    assert explicit["time_to_candidate_seconds"] == 1.0
    assert explicit["time_to_verified_seconds"] == 2.0
    assert explicit["replay_verified"] is True
    assert explicit["data_dependency_verified"] is True
    assert explicit["independent_verified"] is True
    assert explicit["evidence_completed"] is True
    assert explicit["writeup_validated"] is True
    assert explicit["resume_verified"] is True
    assert explicit["total_run_status"] == "DONE"


def test_benchmark_rejects_raw_expected_flag_in_solver_source(tmp_path: Path) -> None:
    solver = tmp_path / "solve.py"
    solver.write_text("print('flag{do_not_embed}')\n", encoding="utf-8")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """repeat_runs: 1
challenges:
  - id: raw-hardcoded
    command: [python3, solve.py]
    expected_flag: flag{do_not_embed}
    clean_replay: false
""",
        encoding="utf-8",
    )

    result = benchmark(manifest)

    run = result["challenges"][0]["runs"][0]
    assert result["solved_count"] == 0
    assert run["hardcoded_rejected"] is True
    assert "raw expected flag" in run["error"]


def test_benchmark_rejects_encoded_expected_flag_in_solver_source(tmp_path: Path) -> None:
    expected = "flag{encoded_secret}"
    encoded = base64.b64encode(expected.encode()).decode()
    solver = tmp_path / "solve.py"
    solver.write_text(
        f"import base64\nprint(base64.b64decode({encoded!r}).decode())\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        f"""repeat_runs: 1
challenges:
  - id: encoded-hardcoded
    command: [python3, solve.py]
    expected_flag: {expected}
    clean_replay: false
""",
        encoding="utf-8",
    )

    result = benchmark(manifest)

    run = result["challenges"][0]["runs"][0]
    assert result["solved_count"] == 0
    assert run["hardcoded_rejected"] is True
    assert "base64 expected flag" in run["error"]


def test_benchmark_separates_fixture_success_from_clean_replay(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("replay_ok\n", encoding="utf-8")
    (tmp_path / "solve.py").write_text(
        """
from pathlib import Path

secret = Path("secret.txt").read_text(encoding="utf-8").strip()
print(f"flag{{{secret}}}")
""",
        encoding="utf-8",
    )
    (tmp_path / "replay.py").write_text("raise SystemExit(2)\n", encoding="utf-8")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """repeat_runs: 1
challenges:
  - id: replay-fails
    command: [python3, solve.py]
    replay_command: [python3, replay.py]
    expected_flag: flag{replay_ok}
""",
        encoding="utf-8",
    )

    result = benchmark(manifest)

    challenge = result["challenges"][0]
    run = challenge["runs"][0]
    assert challenge["fixture_command_success_rate"] == 1
    assert challenge["clean_replay_success_rate"] == 0
    assert result["fixture_command_success_rate"] == 1
    assert result["clean_reproduction_rate"] == 0
    assert run["fixture_command_success"] is True
    assert run["clean_replay_success"] is False
    assert run["solved"] is False


def test_benchmark_supports_expected_flag_hash(tmp_path: Path) -> None:
    expected = "flag{hash_only}"
    (tmp_path / "answer.txt").write_text(expected, encoding="utf-8")
    solver = tmp_path / "solve.py"
    solver.write_text(
        "from pathlib import Path\nprint(Path('answer.txt').read_text(encoding='utf-8'))\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        f"""repeat_runs: 1
challenges:
  - id: hash-only
    command: [python3, solve.py]
    expected_flag_sha256: {hashlib.sha256(expected.encode()).hexdigest()}
    clean_replay: false
""",
        encoding="utf-8",
    )

    result = benchmark(manifest)

    assert result["solved_count"] == 1
    assert result["results"][0]["clean_reproduction"] is False


def test_hash_only_benchmark_rejects_embedded_flag_literal(tmp_path: Path) -> None:
    expected = "flag{hash_embedded}"
    solver = tmp_path / "solve.py"
    solver.write_text(f"print({expected!r})\n", encoding="utf-8")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        f"""repeat_runs: 1
challenges:
  - id: hash-hardcoded
    command: [python3, solve.py]
    expected_flag_sha256: {hashlib.sha256(expected.encode()).hexdigest()}
    clean_replay: false
""",
        encoding="utf-8",
    )

    result = benchmark(manifest)

    run = result["challenges"][0]["runs"][0]
    assert run["hardcoded_rejected"] is True
    assert "matching expected flag hash" in run["error"]


@pytest.mark.parametrize(
    ("command", "filename", "source"),
    [
        (["sh", "solve.sh"], "solve.sh", "printf '%s\\n' 'flag{shell_embedded}'\n"),
        (
            ["node", "solve.js"],
            "solve.js",
            "console.log('flag{' + 'javascript_embedded}')\n",
        ),
    ],
)
def test_hash_only_benchmark_rejects_non_python_literals(
    tmp_path: Path, command: list[str], filename: str, source: str
) -> None:
    expected = (
        "flag{shell_embedded}"
        if filename.endswith(".sh")
        else "flag{javascript_embedded}"
    )
    (tmp_path / filename).write_text(source, encoding="utf-8")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        f"""repeat_runs: 1
challenges:
  - id: hash-hardcoded-non-python
    command: {json.dumps(command)}
    expected_flag_sha256: {hashlib.sha256(expected.encode()).hexdigest()}
    clean_replay: false
""",
        encoding="utf-8",
    )

    result = benchmark(manifest)

    run = result["challenges"][0]["runs"][0]
    assert run["hardcoded_rejected"] is True
    assert "matching expected flag hash" in run["error"]


def test_benchmark_rejects_command_path_outside_fresh_fixture(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    outside = tmp_path / "outside_solver.py"
    outside.write_text("print('flag{escape}')\n", encoding="utf-8")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        f"""repeat_runs: 1
challenges:
  - id: path-escape
    workdir: fixture
    command: [python3, {outside}]
    expected_flag: flag{{escape}}
    clean_replay: false
""",
        encoding="utf-8",
    )

    result = benchmark(manifest)

    run = result["challenges"][0]["runs"][0]
    assert run["solved"] is False
    assert "fresh workdir" in run["error"]


def test_benchmark_timeout_kills_child_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "child-marker.txt"
    solver = tmp_path / "solve.py"
    child_code = (
        "import time; from pathlib import Path; time.sleep(0.3); "
        f"Path({str(marker)!r}).write_text('alive')"
    )
    solver.write_text(
        "import subprocess, sys, time\n"
        f"code={child_code!r}\n"
        "subprocess.Popen([sys.executable, '-c', code])\n"
        "time.sleep(5)\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """repeat_runs: 1
timeout_seconds: 0.1
challenges:
  - id: child-timeout
    command: [python3, solve.py]
    expected_flag: flag{never}
    clean_replay: false
""",
        encoding="utf-8",
    )

    result = benchmark(manifest)
    time.sleep(0.5)

    run = result["challenges"][0]["runs"][0]
    assert run["timed_out"] is True
    assert not marker.exists()


def test_benchmark_rejects_split_python_flag_literal(tmp_path: Path) -> None:
    solver = tmp_path / "solve.py"
    solver.write_text("print('flag{' + 'split_secret}')\n", encoding="utf-8")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """repeat_runs: 1
challenges:
  - id: split-hardcode
    command: [python3, solve.py]
    expected_flag: flag{split_secret}
    clean_replay: false
""",
        encoding="utf-8",
    )

    result = benchmark(manifest)

    run = result["challenges"][0]["runs"][0]
    assert run["hardcoded_rejected"] is True
    assert "constructs raw expected flag" in run["error"]


def test_benchmark_rejects_inline_interpreter_command(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """repeat_runs: 1
challenges:
  - id: inline-hardcode
    command: [python3, -c, "print('flag{inline_secret}')"]
    expected_flag: flag{inline_secret}
    clean_replay: false
""",
        encoding="utf-8",
    )

    result = benchmark(manifest)

    run = result["challenges"][0]["runs"][0]
    assert run["solved"] is False
    assert "inline interpreter execution" in run["error"]


def test_benchmark_rejects_shell_combined_inline_flags(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """repeat_runs: 1
challenges:
  - id: shell-inline
    command: [bash, -lc, "printf 'flag{bash_lc}'"]
    expected_flag: flag{bash_lc}
    clean_replay: false
""",
        encoding="utf-8",
    )

    result = benchmark(manifest)

    run = result["challenges"][0]["runs"][0]
    assert run["solved"] is False
    assert "inline interpreter execution" in run["error"]


def test_benchmark_rejects_node_print_inline_source(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """repeat_runs: 1
challenges:
  - id: node-inline
    command: [node, -p, "'flag{node_print}'"]
    expected_flag: flag{node_print}
    clean_replay: false
""",
        encoding="utf-8",
    )

    result = benchmark(manifest)

    run = result["challenges"][0]["runs"][0]
    assert run["solved"] is False
    assert "inline interpreter execution" in run["error"]
