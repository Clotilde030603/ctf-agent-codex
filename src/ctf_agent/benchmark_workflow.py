"""Deterministic scorer-owned workflow execution for offline benchmark cases."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ctf_agent.benchmark_manifest import BenchmarkChallenge
from ctf_agent.benchmark_offline_adapter import OfflineBenchmarkAdapter
from ctf_agent.benchmark_offline_backend import OfflineBenchmarkBackend
from ctf_agent.capabilities import (
    CapabilityCategory,
    CapabilityProvider,
    CapabilityStatus,
    ContainerProbeResult,
    RuntimeCapabilitySnapshot,
    StaticCapabilityProbe,
    ToolProbeResult,
)
from ctf_agent.capability_manifest import DEFAULT_CAPABILITY_MANIFEST
from ctf_agent.workflow import AutonomousWorkflow


@dataclass(frozen=True, slots=True)
class OfflineWorkflowResult:
    """Authoritative artifacts observed after real controller execution."""

    run_id: str
    run_dir: Path
    final_state: str
    events: tuple[dict[str, object], ...]


async def execute_offline_workflow(
    workflow: AutonomousWorkflow,
    benchmark: BenchmarkChallenge,
    source: Path,
    scorer_root: Path,
) -> OfflineWorkflowResult:
    """Drive the configured workflow through its real controller surface."""
    workflow.settings.runs_dir = scorer_root / "runs"
    workflow._adapter_override = OfflineBenchmarkAdapter(benchmark, source)
    workflow._planner_backend_override = OfflineBenchmarkBackend(
        workflow, "planner", scorer_root
    )
    workflow._solver_backend_factory = (
        lambda _settings, role, cwd: OfflineBenchmarkBackend(workflow, role, cwd)
    )
    workflow._reviewer_backend_factory = (
        lambda _settings, role, cwd: OfflineBenchmarkBackend(workflow, role, cwd)
    )
    workflow._worker_local_test_mode = True
    workflow._worker_allowed_argv0 = {"python3"}
    if workflow._runtime_capabilities is None:
        workflow._runtime_capabilities = _capability_snapshot(workflow)
    controller = workflow.controller()
    context = controller.create_run(
        f"https://benchmark.invalid/challenges/{benchmark.id}",
        auto_submit=False,
        writeup=False,
    )
    result = await controller.execute(context)
    checkpoints = context.store.lane_checkpoints().list(result.run_id)
    budget = workflow._model_budget(context).snapshot()
    capabilities = workflow._runtime_capability_snapshot().capabilities
    context.ledger.append(
        result.run_id,
        "benchmark.runtime_observed",
        {
            "available_capabilities": sum(
                item.status is CapabilityStatus.AVAILABLE for item in capabilities
            ),
            "elastic_budget": workflow.settings.model_budget_mode == "elastic",
            "lane_continuity": workflow.settings.lane_continuity_enabled,
            "context_projection": workflow.settings.context_projection_enabled,
            "adaptive_frontier": workflow.settings.adaptive_frontier_enabled,
            "budget_requested": budget.requested,
            "checkpoint_count": len(checkpoints),
            "checkpoint_resumes": sum(item.revision > 2 for item in checkpoints),
        },
        state=result.state.value,
    )
    events = tuple(
        {
            "type": event["event_type"],
            "elapsed_seconds": float(index),
            "payload": event["payload"],
        }
        for index, event in enumerate(context.ledger.list(result.run_id))
    )
    return OfflineWorkflowResult(
        run_id=result.run_id,
        run_dir=result.run_dir,
        final_state=result.state.value,
        events=events,
    )


def _capability_snapshot(workflow: AutonomousWorkflow) -> RuntimeCapabilitySnapshot:
    corrected = workflow.settings.runtime_capability_mode == "corrected"
    tool_definitions = tuple(
        item
        for item in DEFAULT_CAPABILITY_MANIFEST.capabilities
        if item.category is CapabilityCategory.TOOL
    )
    provider = CapabilityProvider(
        DEFAULT_CAPABILITY_MANIFEST,
        StaticCapabilityProbe(
            ContainerProbeResult(
                reachable=True,
                image_digest=workflow.settings.docker_image,
                tools=tuple(
                    ToolProbeResult(
                        name=item.name,
                        installed=item.name == "python3",
                        reachable=True,
                        authenticated=None,
                        version="3.12" if item.name == "python3" else None,
                        source="scorer-offline-runtime",
                        reason=(
                            "provider-backed capability correction"
                            if item.name == "python3" and corrected
                            else "legacy command policy omits discovered tools"
                            if item.name == "python3"
                            else "command not present in scorer runtime"
                        ),
                    )
                    for item in tool_definitions
                ),
            )
        ),
    )
    return provider.snapshot(
        workflow.settings.docker_image,
        allowed_tools=None if corrected else frozenset(),
    )
