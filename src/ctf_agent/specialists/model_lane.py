"""Lane workspace preparation and controlled worker construction."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ctf_agent.capabilities import RuntimeCapabilitySnapshot
from ctf_agent.execution_receipts import ExecutionReceiptStore
from ctf_agent.ingestion.session import ScopedAsyncSession
from ctf_agent.lanes import LaneCheckpoint, LaneCheckpointStore, stable_lane_id
from ctf_agent.schemas import Hypothesis
from ctf_agent.workers import CommandPolicy, LaneWorkspace, WorkerBudget, WorkerCore

if TYPE_CHECKING:
    from ctf_agent.specialists.model import ModelSolverSpecialist


@dataclass(frozen=True, slots=True)
class PreparedLane:
    run_dir: Path
    lane_dir: Path
    store: LaneCheckpointStore
    checkpoint: LaneCheckpoint
    http_session: ScopedAsyncSession | None
    worker: WorkerCore


def prepare_lane(
    specialist: ModelSolverSpecialist,
    hypothesis: Hypothesis,
    context: dict[str, object],
) -> PreparedLane:
    run_dir = Path(str(context["run_dir"])).resolve()
    run_id = str(context.get("run_id", run_dir.name))
    continuity_enabled = context.get(
        "lane_continuity_enabled", specialist.settings.lane_continuity_enabled
    ) is True
    if not continuity_enabled:
        raw_sequence = context.get("_noncontinuing_lane_sequence", 0)
        sequence = (raw_sequence if isinstance(raw_sequence, int) else 0) + 1
        context["_noncontinuing_lane_sequence"] = sequence
        run_id = f"{run_id}:slice:{sequence}"
    lane_id = str(stable_lane_id(run_id, hypothesis.id, specialist.name))
    lane_dir = run_dir / "artifacts" / "lanes" / lane_id
    observer = context.get("event_observer")
    store = LaneCheckpointStore(
        Path(str(context.get("state_database", run_dir / "state.db"))),
        event_observer=observer if callable(observer) else None,
    )
    checkpoint, reset = store.resume_or_reset(
        specialist._checkpoint_seed(run_id, lane_id, hypothesis, context)
    )
    if reset and lane_dir.exists():
        shutil.rmtree(lane_dir)
    challenge_copy = lane_dir / "files"
    if not challenge_copy.exists():
        source_files = run_dir / "files"
        if source_files.is_dir():
            shutil.copytree(source_files, challenge_copy)
        else:
            challenge_copy.mkdir(parents=True)

    workspace = LaneWorkspace(lane_dir, challenge_files=run_dir / "files")
    raw_capabilities = context.get("runtime_capabilities")
    runtime_capabilities = (
        RuntimeCapabilitySnapshot.model_validate(raw_capabilities)
        if isinstance(raw_capabilities, Mapping) and raw_capabilities
        else None
    )
    policy = CommandPolicy(
        docker_image=specialist.settings.docker_image,
        local_test_mode=specialist.local_test_mode,
        runtime_capabilities=runtime_capabilities,
    )
    if specialist.allowed_argv0 is not None:
        policy.allowed_argv0 = specialist.allowed_argv0
    configured_model_budget = context.get(
        "model_call_budget", specialist.settings.model_call_budget
    )
    model_budget = (
        configured_model_budget
        if isinstance(configured_model_budget, int)
        else specialist.settings.model_call_budget
    )
    session = specialist._http_session(context)
    worker = WorkerCore(
        specialist.backend_factory(specialist.settings, "solver", lane_dir),
        workspace,
        budget=WorkerBudget(
            max_steps=specialist.settings.worker_max_steps,
            max_model_calls=min(specialist.settings.worker_max_steps, model_budget),
            max_commands=specialist.settings.worker_max_commands,
            max_http_requests=specialist.settings.worker_max_http_requests,
            max_wall_time_seconds=specialist.settings.worker_wall_time_seconds,
            command_timeout_seconds=specialist.settings.tool_timeout_seconds,
            max_no_progress_steps=specialist.settings.worker_no_progress_limit,
        ),
        policy=policy,
        model_budget=specialist.model_budget,
        budget_request_prefix=(
            f"{context.get('budget_request_prefix', 'solve')}:{hypothesis.id}:solver"
        ),
        http_session=session,
        event_observer=observer if callable(observer) else None,
        checkpoint_store=store,
        execution_receipts=ExecutionReceiptStore(store.database),
        lane_id=lane_id,
        failpoint=specialist.worker_failpoint,
    )
    return PreparedLane(
        run_dir=run_dir,
        lane_dir=lane_dir,
        store=store,
        checkpoint=checkpoint,
        http_session=session,
        worker=worker,
    )
