"""Extracted workflow behavior."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ctf_agent.workflow import AutonomousWorkflow

from ctf_agent.budget_types import ArtifactProgress, CandidateReceipt, ProgressEvidence
from ctf_agent.engine import RunContext, StateOutcome
from ctf_agent.scheduler import Scheduler, StaticHypothesisPlanner
from ctf_agent.schemas import (
    Hypothesis,
    RunState,
    SpecialistResult,
)
from ctf_agent.security import secure_write_text
from ctf_agent.specialists.deterministic import ArtifactSignalSpecialist
from ctf_agent.specialists.model import ModelSolverSpecialist
from ctf_agent.workflow_parts.io import _write_json


async def solve(workflow: AutonomousWorkflow, context: RunContext) -> StateOutcome:
    hypotheses = context.values.get("hypotheses")
    if not isinstance(hypotheses, list):
        hypotheses = [
            Hypothesis.model_validate(item)
            for item in workflow._load_json(context.record.run_dir / "hypotheses.json")
        ]
        context.values["hypotheses"] = hypotheses
    hypotheses = hypotheses[: workflow.settings.frontier_total_pool]
    context.values["hypotheses"] = hypotheses
    triage_data = context.values.get("triage") or workflow._load_json(
        context.record.run_dir / "triage.json"
    )
    solver_context = workflow._solver_context(context, triage_data)
    preflight = await ArtifactSignalSpecialist().solve(hypotheses[0], solver_context)
    preliminary_results = [preflight]
    if not (preflight.status == "confirmed" and preflight.flag_candidates):
        category_specialist = workflow._category_specialist(
            triage_data, workflow._runtime_capability_snapshot()
        )
        category_result = None
        if category_specialist is not None:
            category_result = await category_specialist.solve(hypotheses[0], solver_context)
            preliminary_results.append(category_result)
    preliminary_solved = any(
        result.status == "confirmed" and result.flag_candidates for result in preliminary_results
    )
    if workflow.settings.backend != "codex":
        specialist_results = tuple(preliminary_results)
        solved = preliminary_solved
        stop_reason = "static_preflight" if solved else "no_model_backend"
    else:
        solver_context["preflight_results"] = [
            result.model_dump(mode="json") for result in preliminary_results
        ]
        model_budget = workflow._model_budget(context)
        solve_round = 1 + sum(
            event["event_type"] == "solve.round"
            for event in context.ledger.list(context.record.run_id)
        )
        solver_context["budget_request_prefix"] = f"{context.record.run_id}:solve:{solve_round}"
        solver_context["lane_slice_max_steps"] = workflow.settings.lane_quantum_steps
        solver_context["provisional_candidates"] = True
        solver_context["frontier_events"] = [
            event["payload"]
            for event in context.ledger.list(context.record.run_id)
            if event["event_type"].startswith(("frontier.", "lane.", "candidate."))
        ]

        def record_solver_event(event_type: str, payload: Mapping[str, Any]) -> None:
            context.ledger.append(
                context.record.run_id,
                event_type,
                dict(payload) | {"model": workflow.settings.solver_model},
                state=RunState.SOLVE.value,
                idempotency_key=(
                    "flag-candidate:" + str(payload.get("candidate_sha256"))
                    if event_type == "flag.candidate" and payload.get("candidate_sha256")
                    else None
                ),
            )

        solver_context["event_observer"] = record_solver_event

        def collect_progress_evidence(
            results: tuple[SpecialistResult, ...],
        ) -> ProgressEvidence:
            reported_facts = {fact for result in results for fact in result.facts}
            verified_facts = tuple(
                fact
                for checkpoint in context.store.lane_checkpoints().list(context.record.run_id)
                for fact in checkpoint.facts
                if fact.status == "validated" and fact.fact in reported_facts
            )
            root = context.record.run_dir.resolve()
            artifact_paths = tuple(
                (root / artifact).resolve()
                for result in results
                for artifact in result.artifacts
            )
            artifacts = tuple(
                ArtifactProgress(path, hashlib.sha256(path.read_bytes()).hexdigest())
                for path in artifact_paths
                if path.is_file() and root in path.parents
            )
            receipts = tuple(
                CandidateReceipt(hashlib.sha256(candidate.value.encode()).hexdigest())
                for result in results
                for candidate in result.flag_candidates
                if candidate.reproduction_spec is not None
            )
            return ProgressEvidence(verified_facts, artifacts, receipts)

        solver_context["progress_evidence_provider"] = collect_progress_evidence
        solver_context["budget_extension_decider"] = model_budget.extend
        model_specialist = ModelSolverSpecialist(
            workflow.settings,
            backend_factory=workflow._solver_backend_factory,
            local_test_mode=workflow._worker_local_test_mode,
            allowed_argv0=workflow._worker_allowed_argv0,
            model_budget=model_budget,
            auth_broker=workflow._auth_broker,
        )
        scheduler = Scheduler(
            StaticHypothesisPlanner(hypotheses),
            (model_specialist,),
            no_progress_cutoff=3,
            max_rounds=workflow.settings.frontier_max_rounds,
            max_concurrency=workflow.settings.frontier_active_width,
            adaptive_frontier=workflow.settings.adaptive_frontier_enabled,
        )
        result = await scheduler.run(solver_context)
        specialist_results = tuple(preliminary_results) + result.specialist_results
        has_provisional_candidate = any(
            item.status == "confirmed" and item.flag_candidates
            for item in result.specialist_results
        )
        solved = result.solved or has_provisional_candidate or preliminary_solved
        context.values["adaptive_frontier"] = has_provisional_candidate
        stop_reason = (
            "solved"
            if has_provisional_candidate
            else result.stop_reason
            if result.solved or not preliminary_solved
            else "model_reviewed_preflight_candidate"
        )
        model_budget.finish(stop_reason)
        budget_path = context.record.run_dir / "artifacts" / "model-budget.json"
        _write_json(budget_path, model_budget.snapshot().to_dict())
    filtered_results: list[SpecialistResult] = []
    for item in specialist_results:
        candidates = [
            candidate
            for candidate in item.flag_candidates
            if not context.store.is_rejected(context.record.run_id, candidate.value)
        ]
        filtered_results.append(
            item.model_copy(
                update={
                    "status": "confirmed" if candidates else "inconclusive",
                    "flag_candidates": candidates,
                }
            )
            if candidates != item.flag_candidates
            else item
        )
    specialist_results = tuple(filtered_results)
    solved = any(item.status == "confirmed" and item.flag_candidates for item in specialist_results)
    specialist_results = workflow._promote_solver(context.record.run_dir, specialist_results)
    _write_json(
        context.record.run_dir / "artifacts" / "specialist-results.json",
        [item.model_dump(mode="json") for item in specialist_results],
    )
    requirements = context.record.run_dir / "requirements.txt"
    if not requirements.exists():
        secure_write_text(
            requirements,
            "# Solver dependencies were not declared by the selected lane.\n",
        )
    context.values["specialist_results"] = list(specialist_results)
    context.ledger.append(
        context.record.run_id,
        "solve.round",
        {
            "stop_reason": stop_reason,
            "solved": solved,
            "results": [
                {
                    "hypothesis_id": result.hypothesis_id,
                    "status": result.status,
                    "facts": result.facts,
                    "artifacts": result.artifacts,
                    "commands": result.commands,
                    "next_action": result.next_action,
                    "candidate_count": len(result.flag_candidates),
                }
                for result in specialist_results
            ],
        },
        state=RunState.SOLVE.value,
    )
    if not solved:
        return StateOutcome(
            RunState.SOLVE if stop_reason == "progress" else RunState.PLAN,
            {"solved": False, "stop_reason": stop_reason},
        )
    return StateOutcome(RunState.VERIFY, {"stop_reason": stop_reason})
