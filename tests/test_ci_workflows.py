from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def _workflow(path: str):
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def _step_script(workflow, job: str, step_name: str) -> str:
    steps = workflow["jobs"][job]["steps"]
    return next(step["run"] for step in steps if step.get("name") == step_name)


def test_ci_runs_for_pull_requests_and_v3_base_pushes_only() -> None:
    workflow = _workflow(".github/workflows/ci.yml")

    triggers = workflow["on"]

    assert "pull_request" in triggers
    assert triggers["push"] == {"branches": ["feat/autonomous-ctf-agent-v3"]}


def test_package_ci_excludes_benchmark_workloads() -> None:
    workflow = _workflow(".github/workflows/ci.yml")

    script = _step_script(workflow, "package", "Run package checks when implemented")

    assert "ctf-agent benchmark" not in script


def test_package_ci_excludes_slow_and_benchmark_tests() -> None:
    workflow = _workflow(".github/workflows/ci.yml")

    script = _step_script(workflow, "package", "Run package checks when implemented")

    assert 'pytest -m "not benchmark and not slow"' in script


def test_package_ci_keeps_standard_validation_and_cli_smoke() -> None:
    workflow = _workflow(".github/workflows/ci.yml")

    package_script = _step_script(
        workflow, "package", "Run package checks when implemented"
    )
    pipx_script = _step_script(
        workflow, "package", "Verify pipx installation and CLI entry point"
    )

    assert "python -m pip install -e \".[dev]\"" in package_script
    assert "ruff check ." in package_script
    assert "mypy src" in package_script
    assert "python -m compileall -q src tests evals" in package_script
    assert "uv build --out-dir /tmp/ctf-agent-dist" in package_script
    assert "pipx install . --force" in pipx_script
    for command in (
        "--help",
        "solve --help",
        "resume --help",
        "retry-evidence --help",
        "benchmark --help",
        "doctor --help",
    ):
        assert f'"$PIPX_CLI" {command}' in pipx_script


def test_full_benchmark_has_nightly_manual_release_triggers() -> None:
    path = ROOT / ".github/workflows/full-benchmark.yml"
    assert path.is_file(), "dedicated full benchmark workflow is missing"
    workflow = _workflow(".github/workflows/full-benchmark.yml")

    triggers = workflow["on"]
    script = _step_script(workflow, "full-benchmark", "Run full B0-B5 benchmark")
    steps = workflow["jobs"]["full-benchmark"]["steps"]

    assert set(triggers) == {"schedule", "workflow_dispatch", "release"}
    assert triggers["schedule"][0]["cron"]
    assert triggers["release"]["types"] == ["published"]
    assert "ctf-agent benchmark evals/manifest.v2.yaml" in script
    assert "--ablation-matrix evals/ablations.yaml" in script
    assert any(
        step.get("uses", "").startswith("actions/upload-artifact@") for step in steps
    )
