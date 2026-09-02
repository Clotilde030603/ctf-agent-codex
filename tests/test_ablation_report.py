from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from ctf_agent.benchmark import BenchmarkManifest
from ctf_agent.cli import app

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "evals" / "manifest.v2.yaml"
MATRIX = ROOT / "evals" / "ablations.yaml"


def test_evaluation_identity_and_case_provenance_are_frozen() -> None:
    payload = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))

    manifest = BenchmarkManifest.model_validate(payload)

    assert 12 <= len(manifest.challenges) <= 20
    assert payload["evaluation_id"] == "g010-local-pilot-v1"
    assert payload["dataset_revision"] == "local-pilot-2026-09-01"
    assert payload["ablation_revision"] == "b0-b5-v2"
    assert len({case["case_id"] for case in payload["challenges"]}) == len(
        manifest.challenges
    )
    for case in payload["challenges"]:
        assert len(case["fixture_sha256"]) == 64
        assert len(case["solution_sha256"]) == 64
        assert "/blob/main/" not in case["redistribution"]["evidence_url"]


@pytest.mark.benchmark
def test_cli_report_has_paired_conditions_cost_latency_and_contamination(
    tmp_path: Path,
) -> None:
    output = tmp_path / "report.json"

    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            str(MANIFEST),
            "--ablation-matrix",
            str(MATRIX),
            "--output",
            str(output),
            "--solve-k",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text(encoding="utf-8"))
    assert 12 <= len(report["challenge_identities"]) <= 20
    assert [item["condition_id"] for item in report["condition_summaries"]] == [
        "B0",
        "B1",
        "B2",
        "B3",
        "B4",
        "B5",
    ]
    assert report["pairing"]["complete"] is True
    assert report["solve_at"]["1"]["denominator"] == 12
    assert report["solve_at"]["3"]["denominator"] == 12
    assert report["solve_at"]["2"]["denominator"] == 12
    assert report["costs"]["model"] > 216.0
    assert report["costs"]["tool"] > 0
    assert report["costs"]["network"] == 0.0
    assert report["costs"]["total"] == (
        report["costs"]["model"]
        + report["costs"]["tool"]
        + report["costs"]["network"]
    )
    assert report["costs"]["unit"] == "scorer_event_units"
    assert report["latency_seconds"]["median"] >= 0
    assert report["latency_seconds"]["p95"] >= 0
    assert report["latency_seconds"]["iqr"]["q1"] >= 0
    assert report["latency_seconds"]["iqr"]["q3"] >= report["latency_seconds"]["iqr"]["q1"]
    assert set(report["solve_at"]["1"]["confidence_interval"]) == {"low", "high"}
    assert report["failure_reasons"]
    assert any("python3" in reason for reason in report["failure_reasons"])
    assert report["operational_metrics"]["context_bytes"] >= 0
    assert set(report["operational_metrics"]) >= {
        "model_starvation_count",
        "repeated_action_count",
        "lane_retirement_count",
        "lane_replacement_count",
        "tcp_connect_count",
        "restart_count",
        "recovery_count",
    }
    assert report["empirical_provenance_identities"]
    assert report["contamination"]["interpretation"] == (
        "disclosed separately; solve metrics are not adjusted"
    )
    assert len(report["paired_deltas"]) == 5
    b0 = next(item for item in report["runs"] if item["identity"]["condition_id"] == "B0")
    b5 = next(item for item in report["runs"] if item["identity"]["condition_id"] == "B5")
    assert b0["observed_runtime_identity"] == {
        **b0["observed_runtime_identity"],
        "capability_mode": "current",
        "budget_mode": "shared",
        "lane_continuity": False,
        "context_projection": False,
        "frontier_mode": "fixed",
    }
    assert b5["observed_runtime_identity"] == {
        **b5["observed_runtime_identity"],
        "capability_mode": "corrected",
        "budget_mode": "elastic",
        "lane_continuity": True,
        "context_projection": True,
        "frontier_mode": "adaptive",
    }
    assert b0["observed_runtime_identity"] != b5["observed_runtime_identity"]
    assert b0["event_metrics"] != b5["event_metrics"]
    assert b0["solved"] is False
    assert b5["solved"] is True


@pytest.mark.benchmark
def test_report_is_deterministic_for_same_seed_and_identities(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    runner = CliRunner()

    first_result = runner.invoke(
        app,
        ["benchmark", str(MANIFEST), "--ablation-matrix", str(MATRIX), "-o", str(first)],
    )
    second_result = runner.invoke(
        app,
        ["benchmark", str(MANIFEST), "--ablation-matrix", str(MATRIX), "-o", str(second)],
    )

    assert first_result.exit_code == 0, first_result.output
    assert second_result.exit_code == 0, second_result.output
    assert first.read_bytes() == second.read_bytes()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"evaluation_id": "stale-evaluation"}, "evaluation_id mismatch"),
        ({"dataset_revision": "stale-dataset"}, "dataset_revision mismatch"),
        ({"ablation_revision": "stale-ablation"}, "ablation_revision mismatch"),
        ({"conditions": []}, "exactly B0-B5"),
    ],
)
def test_missing_or_mismatched_matrix_identity_is_rejected(
    tmp_path: Path, mutation: dict[str, str | list[str]], message: str
) -> None:
    payload = yaml.safe_load(MATRIX.read_text(encoding="utf-8"))
    payload.update(mutation)
    changed = tmp_path / "ablations.yaml"
    changed.write_text(yaml.safe_dump(payload), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["benchmark", str(MANIFEST), "--ablation-matrix", str(changed)],
    )

    assert result.exit_code != 0
    assert message in result.output


def test_condition_cannot_relabel_an_incorrect_feature_progression(
    tmp_path: Path,
) -> None:
    # Given: B0 is relabeled with adaptive-frontier behavior and a matching new hash.
    import hashlib

    payload = yaml.safe_load(MATRIX.read_text(encoding="utf-8"))
    condition = payload["conditions"][0]
    condition["frontier_mode"] = "adaptive"
    condition.pop("config_sha256")
    condition["config_sha256"] = hashlib.sha256(
        json.dumps(condition, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    changed = tmp_path / "ablations.yaml"
    changed.write_text(yaml.safe_dump(payload), encoding="utf-8")

    # When: the scorer loads the frozen matrix.
    result = CliRunner().invoke(
        app,
        ["benchmark", str(MANIFEST), "--ablation-matrix", str(changed)],
    )

    # Then: a hash-consistent but specification-inconsistent condition is rejected.
    assert result.exit_code != 0
    assert "cumulative #3-#7 specification" in result.output


def test_config_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    payload = yaml.safe_load(MATRIX.read_text(encoding="utf-8"))
    payload["conditions"][0]["model_id"] = "changed-model"
    changed = tmp_path / "ablations.yaml"
    changed.write_text(yaml.safe_dump(payload), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["benchmark", str(MANIFEST), "--ablation-matrix", str(changed)],
    )

    assert result.exit_code != 0
    assert "config_sha256 mismatch" in result.output


def test_fixture_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    payload = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    payload["challenges"][0]["fixture_sha256"] = "0" * 64
    changed = tmp_path / "manifest.v2.yaml"
    changed.write_text(yaml.safe_dump(payload), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["benchmark", str(changed), "--ablation-matrix", str(MATRIX)],
    )

    assert result.exit_code != 0
    assert "fixture_sha256 mismatch" in result.output
