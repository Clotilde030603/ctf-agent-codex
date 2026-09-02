"""Scorer-owned autonomous execution authority."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from ctf_agent.ablation_schema import AblationCondition, ObservedRuntimeIdentity
from ctf_agent.benchmark_manifest import BenchmarkChallenge
from ctf_agent.benchmark_metrics import _derive_event_metrics
from ctf_agent.benchmark_models import BenchmarkMetrics
from ctf_agent.benchmark_workflow import _capability_snapshot, execute_offline_workflow
from ctf_agent.capabilities import RuntimeCapabilitySnapshot
from ctf_agent.config import Settings
from ctf_agent.schemas import FlagCandidate, RunState
from ctf_agent.state import StateStore
from ctf_agent.workflow import AutonomousWorkflow


class AutonomousArtifactError(ValueError):
    """A scorer-owned autonomous invocation is absent or inconsistent."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class ScorerInvocation:
    """Unforgeable in-memory authority bound to the executable workflow."""

    observed_runtime_identity: ObservedRuntimeIdentity
    _workflow: AutonomousWorkflow = field(repr=False, compare=False)
    _nonce: bytes = field(repr=False)

    @classmethod
    def create(cls, condition: AblationCondition) -> ScorerInvocation:
        elastic = condition.budget_mode == "elastic"
        settings = Settings(
            backend="codex",
            planner_model=condition.model_id,
            solver_model=condition.model_id,
            verifier_model=condition.model_id,
            planner_effort=condition.reasoning_id,
            solver_effort=condition.reasoning_id,
            verifier_effort=condition.reasoning_id,
            docker_image=condition.tool_image_digest,
            runtime_capability_mode=condition.capability_mode,
            model_budget_mode=condition.budget_mode,
            model_call_budget=60,
            model_budget_hard_limit=64 if elastic else 60,
            model_budget_max_extensions=2 if elastic else 0,
            lane_continuity_enabled=condition.lane_continuity,
            context_projection_enabled=condition.context_projection,
            adaptive_frontier_enabled=condition.frontier_mode == "adaptive",
            allow_local_reproduction=True,
            worker_max_steps=6,
            worker_max_commands=2,
            frontier_total_pool=6,
            frontier_active_width=3,
            frontier_max_rounds=3,
        )
        workflow = AutonomousWorkflow(settings)
        snapshot = _capability_snapshot(workflow)
        workflow._runtime_capabilities = snapshot
        observed = _observed_identity(workflow, condition, snapshot)
        expected = ObservedRuntimeIdentity.model_validate(
            condition.model_dump(
                include={
                    "capability_mode",
                    "budget_mode",
                    "lane_continuity",
                    "context_projection",
                    "frontier_mode",
                    "model_id",
                    "reasoning_id",
                    "tool_image_digest",
                    "capability_snapshot_digest",
                    "skill_ids",
                    "solver_id",
                    "artifact_id",
                    "seed",
                    "config_sha256",
                }
            )
        )
        if observed.capability_snapshot_digest != expected.capability_snapshot_digest:
            raise AutonomousArtifactError(
                "observed capability snapshot digest differs from frozen condition"
            )
        if observed != expected:
            raise AutonomousArtifactError(
                "observed runtime identity differs from frozen condition"
            )
        return cls(observed, workflow, secrets.token_bytes(32))

    @classmethod
    def create_default(
        cls, model_id: str, reasoning_id: str, image: str
    ) -> ScorerInvocation:
        payload: dict[str, object] = {
            "condition_id": "B5",
            "description": "Default complete workflow",
            "capability_mode": "corrected",
            "budget_mode": "elastic",
            "lane_continuity": True,
            "context_projection": True,
            "frontier_mode": "adaptive",
            "model_id": model_id,
            "reasoning_id": reasoning_id,
            "tool_image_digest": image,
            "capability_snapshot_digest": _capability_snapshot(
                AutonomousWorkflow(Settings(docker_image=image))
            ).digest,
            "skill_ids": (),
            "solver_id": "manifest-command",
            "artifact_id": "manifest-artifact",
            "seed": 0,
        }
        payload["config_sha256"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return cls.create(AblationCondition.model_validate(payload))

    @property
    def invocation_id(self) -> str:
        return hashlib.sha256(self._nonce).hexdigest()


@dataclass(frozen=True, slots=True)
class AutonomousScoreRequest:
    """Scorer-known fixture identity for one autonomous execution."""

    challenge: BenchmarkChallenge
    source_artifact: str


@dataclass(frozen=True, slots=True)
class AutonomousArtifacts:
    """Facts observed or produced by scorer-owned workflow code."""

    run_id: str
    final_state: str
    candidate: FlagCandidate | None
    verified_candidate: bool
    promoted_solver_sha256: str | None
    metrics: BenchmarkMetrics
    observed_runtime_identity: ObservedRuntimeIdentity
    run_dir: Path
    error: str | None = None


async def run_autonomous_workflow(
    fixture_dir: Path,
    invocation: ScorerInvocation,
    request: AutonomousScoreRequest,
) -> AutonomousArtifacts:
    """Execute and score one real workflow without command-owned authority."""
    source = (fixture_dir / request.source_artifact).resolve()
    try:
        source.relative_to(fixture_dir.resolve())
    except ValueError as exc:
        raise AutonomousArtifactError("source artifact escapes run directory") from exc
    if not source.is_file():
        raise AutonomousArtifactError("scorer-observed source artifact is missing")
    execution = await execute_offline_workflow(
        invocation._workflow,
        request.challenge,
        source,
        fixture_dir / ".scorer",
    )
    store = StateStore(execution.run_dir / "state.db")
    verified = store.load_verified_candidate(execution.run_id)
    solver = execution.run_dir / "solve.py"
    metrics = BenchmarkMetrics.model_validate(_derive_event_metrics(execution.events))
    if verified is None:
        return AutonomousArtifacts(
            run_id=execution.run_id,
            final_state=execution.final_state,
            candidate=None,
            verified_candidate=False,
            promoted_solver_sha256=(
                hashlib.sha256(solver.read_bytes()).hexdigest()
                if solver.is_file()
                else None
            ),
            metrics=metrics,
            observed_runtime_identity=invocation.observed_runtime_identity,
            run_dir=execution.run_dir,
            error=(
                "workflow produced no verified candidate: required capability python3 "
                "is disallowed"
                if invocation.observed_runtime_identity.capability_mode == "current"
                else "workflow produced no verified candidate"
            ),
        )
    return AutonomousArtifacts(
        run_id=execution.run_id,
        final_state=execution.final_state,
        candidate=verified.candidate,
        verified_candidate=(
            verified.valid
            and verified.candidate.independent_verified
            and execution.final_state == RunState.READY.value
        ),
        promoted_solver_sha256=hashlib.sha256(solver.read_bytes()).hexdigest(),
        metrics=metrics,
        observed_runtime_identity=invocation.observed_runtime_identity,
        run_dir=execution.run_dir,
    )


def score_autonomous_artifacts(
    run_dir: Path,
    invocation: ScorerInvocation | None = None,
    request: AutonomousScoreRequest | None = None,
) -> None:
    """Reject the removed command-artifact scoring path."""
    del run_dir, request
    if invocation is None:
        raise AutonomousArtifactError(
            "autonomous scoring requires a scorer-owned invocation"
        )
    raise AutonomousArtifactError(
        "command-owned artifacts are not an autonomous scoring authority"
    )


def _observed_identity(
    workflow: AutonomousWorkflow,
    condition: AblationCondition,
    snapshot: RuntimeCapabilitySnapshot,
) -> ObservedRuntimeIdentity:
    return ObservedRuntimeIdentity(
        capability_mode=workflow.settings.runtime_capability_mode,
        budget_mode=workflow.settings.model_budget_mode,
        lane_continuity=workflow.settings.lane_continuity_enabled,
        context_projection=workflow.settings.context_projection_enabled,
        frontier_mode=(
            "adaptive" if workflow.settings.adaptive_frontier_enabled else "fixed"
        ),
        model_id=workflow.settings.solver_model,
        reasoning_id=workflow.settings.solver_effort,
        tool_image_digest=snapshot.image_digest or snapshot.docker_image,
        capability_snapshot_digest=snapshot.digest,
        skill_ids=condition.skill_ids,
        solver_id=condition.solver_id,
        artifact_id=condition.artifact_id,
        seed=condition.seed,
        config_sha256=condition.config_sha256,
    )
