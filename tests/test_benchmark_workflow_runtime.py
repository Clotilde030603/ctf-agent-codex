from __future__ import annotations

from pathlib import Path

import pytest

from ctf_agent.ablation_runner import _load_matrix
from ctf_agent.benchmark import _load_manifest, _run_once
from ctf_agent.benchmark_offline_backend import OfflineBenchmarkBackend
from ctf_agent.benchmark_runner import AutonomousArtifactError, ScorerInvocation
from ctf_agent.config import Settings
from ctf_agent.engine import Controller
from ctf_agent.models.base import ModelRequest
from ctf_agent.workflow import AutonomousWorkflow

ROOT = Path(__file__).parents[1]
PILOT_MANIFEST = ROOT / "evals" / "manifest.v2.yaml"
PILOT_MATRIX = ROOT / "evals" / "ablations.yaml"


def test_benchmark_rejects_matrix_supplied_capability_digest_as_observation() -> None:
    # Given: a frozen condition claims a digest that the provider did not observe.
    condition = _load_matrix(PILOT_MATRIX).conditions[0].model_copy(
        update={"capability_snapshot_digest": "0" * 64}
    )

    # When / Then: scorer creation compares against provider truth and rejects drift.
    with pytest.raises(AutonomousArtifactError, match="capability snapshot"):
        ScorerInvocation.create(condition)


@pytest.mark.asyncio
async def test_autonomous_benchmark_executes_the_real_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: one frozen autonomous case and scorer-owned controller call observers.
    manifest = _load_manifest(PILOT_MANIFEST)
    challenge = manifest.challenges[0]
    condition = _load_matrix(PILOT_MATRIX).conditions[0]
    calls = {"create_run": 0, "execute": 0}
    original_create_run = Controller.create_run
    original_execute = Controller.execute

    def observed_create_run(
        self: Controller, url: str, *, auto_submit: bool, writeup: bool
    ):
        calls["create_run"] += 1
        return original_create_run(
            self, url, auto_submit=auto_submit, writeup=writeup
        )

    async def observed_execute(self: Controller, context):
        calls["execute"] += 1
        return await original_execute(self, context)

    monkeypatch.setattr(Controller, "create_run", observed_create_run)
    monkeypatch.setattr(Controller, "execute", observed_execute)

    # When: the scorer executes one autonomous benchmark attempt.
    record = await _run_once(
        PILOT_MANIFEST,
        challenge,
        1,
        timeout_seconds=30,
        condition=condition,
    )

    # Then: both real controller boundaries participate exactly once and produce an outcome.
    assert calls == {"create_run": 1, "execute": 1}
    assert record.run_identity is not None
    assert record.observed_runtime_identity is not None
    assert record.solved is False
    assert record.final_state is not None


@pytest.mark.asyncio
async def test_autonomous_metrics_are_derived_from_workflow_events() -> None:
    # Given: one scorer-owned autonomous attempt.
    manifest = _load_manifest(PILOT_MANIFEST)
    condition = _load_matrix(PILOT_MATRIX).conditions[0]

    # When: the attempt is scored.
    record = await _run_once(
        PILOT_MANIFEST,
        manifest.challenges[0],
        1,
        timeout_seconds=30,
        condition=condition,
    )

    # Then: legacy synthetic constants cannot constitute the observed metrics.
    legacy_constants = {
        "model_calls": 1,
        "tool_calls": 2,
        "time_to_candidate_seconds": 0.4,
        "time_to_verified_seconds": 0.4,
        "model_cost": 1.0,
        "tool_cost": 0.5,
    }
    assert {
        key: getattr(record.metrics, key) for key in legacy_constants
    } != legacy_constants


@pytest.mark.asyncio
async def test_b0_and_b5_exhibit_distinct_runtime_event_metrics() -> None:
    # Given: identical case/repeat inputs under the two endpoint conditions.
    manifest = _load_manifest(PILOT_MANIFEST)
    matrix = _load_matrix(PILOT_MATRIX)
    challenge = manifest.challenges[0]

    # When: both conditions execute.
    b0 = await _run_once(
        PILOT_MANIFEST,
        challenge,
        1,
        timeout_seconds=30,
        condition=matrix.conditions[0],
    )
    b5 = await _run_once(
        PILOT_MANIFEST,
        challenge,
        1,
        timeout_seconds=30,
        condition=matrix.conditions[-1],
    )

    # Then: feature gates produce distinct observed event metrics, identities, and outcomes.
    assert b0.metrics != b5.metrics
    assert b0.observed_runtime_identity is not None
    assert b5.observed_runtime_identity is not None
    assert b0.observed_runtime_identity != b5.observed_runtime_identity
    assert b0.solved is False
    assert b5.solved is True
    assert b0.error is not None
    assert "python" in b0.error.lower()


@pytest.mark.asyncio
async def test_offline_projection_is_stable_when_tool_runtime_varies(tmp_path: Path) -> None:
    # Given: semantically identical benchmark contexts with volatile tool timings.
    backend = OfflineBenchmarkBackend(
        AutonomousWorkflow(Settings(context_projection_enabled=True)),
        "planner",
        tmp_path,
    )
    request = ModelRequest(
        role="planner",
        prompt="Plan from the observed artifact.",
        context={
            "triage": {
                "files": [
                    {
                        "tool_results": [
                            {"tool": "file", "duration_seconds": 0.001},
                        ]
                    }
                ]
            }
        },
    )
    slower_request = ModelRequest(
        role=request.role,
        prompt=request.prompt,
        context={
            "triage": {
                "files": [
                    {
                        "tool_results": [
                            {"tool": "file", "duration_seconds": 0.123456},
                        ]
                    }
                ]
            }
        },
    )

    # When: the scorer projects both contexts through the offline model boundary.
    first = await backend.complete(request)
    second = await backend.complete(slower_request)

    # Then: runtime timing does not alter the rendered prompt identity or byte metric.
    assert first.metadata["projection_manifest"]["final_sha256"] == second.metadata[
        "projection_manifest"
    ]["final_sha256"]
    assert first.metadata["projection_manifest"]["final_bytes"] == second.metadata[
        "projection_manifest"
    ]["final_bytes"]
