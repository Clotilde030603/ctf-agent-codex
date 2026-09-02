"""Extracted workflow behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ctf_agent.workflow import AutonomousWorkflow

import time

from ctf_agent.budget_types import (
    BudgetExhaustedError,
    BudgetPurpose,
    BudgetRequest,
    BudgetRequestId,
    BudgetRole,
)
from ctf_agent.engine import RunContext, StateOutcome
from ctf_agent.models.base import ModelBackendError
from ctf_agent.scheduler import ModelHypothesisPlanner
from ctf_agent.schemas import (
    Hypothesis,
    RunState,
)
from ctf_agent.workflow_parts.io import _write_json


async def plan(workflow: AutonomousWorkflow, context: RunContext) -> StateOutcome:
    classification = context.values.get("classification")
    triage_data = workflow._load_json(context.record.run_dir / "triage.json")
    triage_classification = triage_data.get("classification", {})
    category = getattr(
        classification,
        "primary_category",
        triage_classification.get("primary_category", "misc"),
    )
    skill_selection = workflow._skill_selection(context, str(category))
    evidence = [
        str(item.get("reason"))
        for item in triage_data.get("classification", {}).get("evidence", [])[:5]
        if isinstance(item, dict)
    ]
    fallback_hypotheses = [
        Hypothesis(
            id=f"H{index}",
            claim=claim,
            supporting_evidence=evidence,
            expected_signal="a provenance-backed flag candidate or a falsifying result",
            cost=cost,
            confidence=confidence,
            required_tools=tools,
            kill_condition="three consecutive runs add no fact or artifact",
            success_condition="fresh replay reproduces a policy-matching candidate",
        )
        for index, (claim, cost, confidence, tools) in enumerate(
            [
                (
                    f"deterministic {category} artifact signals expose the solution",
                    "low",
                    0.65,
                    [],
                ),
                (
                    f"a category-specific {category} transformation is required",
                    "medium",
                    0.45,
                    [category],
                ),
                ("a secondary or mixed-category path explains the challenge", "high", 0.25, []),
                (
                    "metadata and format edge cases conceal a deterministic signal",
                    "low",
                    0.35,
                    [],
                ),
                (
                    "an alternate solver construction can falsify the leading path",
                    "medium",
                    0.3,
                    [],
                ),
                (
                    "a cross-category dependency requires independent reconstruction",
                    "high",
                    0.2,
                    [],
                ),
            ],
            start=1,
        )
    ][: workflow.settings.frontier_total_pool]
    hypotheses = fallback_hypotheses
    planner_source = "static"
    if workflow.settings.backend == "codex":
        budget = workflow._model_budget(context)
        request_index = budget.snapshot().requested + 1
        prior_plans = sum(
            event["event_type"] == "model.completed" and event["payload"].get("role") == "planner"
            for event in context.ledger.list(context.record.run_id)
        )
        purpose = BudgetPurpose.REPLAN if prior_plans else BudgetPurpose.PLAN
        request = BudgetRequest(
            BudgetRole.PLANNER,
            purpose,
            BudgetRequestId(f"{context.record.run_id}:planner:{request_index}"),
        )
        try:
            lease = await budget.acquire(request)
        except BudgetExhaustedError as exc:
            if not workflow.settings.allow_static_fallback:
                raise RuntimeError("model call budget exhausted before planning") from exc
            context.ledger.append(
                context.record.run_id,
                "model.fallback",
                {"role": "planner", "reason": str(exc)},
                state=RunState.PLAN.value,
            )
        else:
            projection_role = "replan" if purpose is BudgetPurpose.REPLAN else "planner"
            planner = ModelHypothesisPlanner(
                workflow._planner_backend(context, projection_role),
                max_hypotheses=workflow.settings.frontier_total_pool,
                skills=skill_selection,
                role=projection_role,
            )
            await budget.start(lease.lease_id)
            context.ledger.append(
                context.record.run_id,
                "model.request",
                {
                    "role": "planner",
                    "purpose": purpose.value,
                    "request_id": lease.request_id,
                    "model": workflow.settings.planner_model,
                    "request_index": request_index,
                },
                state=RunState.PLAN.value,
            )
            try:
                planner_started = time.monotonic()
                hypotheses = list(
                    await planner.plan(workflow._planning_context(context, triage_data))
                )
            except ModelBackendError as exc:
                await budget.commit(lease.lease_id)
                context.ledger.append(
                    context.record.run_id,
                    "model.failure",
                    {
                        "role": "planner",
                        "request_id": lease.request_id,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "elapsed_seconds": round(time.monotonic() - planner_started, 6),
                    },
                    state=RunState.PLAN.value,
                )
                if not workflow.settings.allow_static_fallback:
                    raise
            else:
                await budget.commit(lease.lease_id)
                planner_source = "model"
                context.ledger.append(
                    context.record.run_id,
                    "model.completed",
                    {
                        "role": "planner",
                        "request_id": lease.request_id,
                        "model": workflow.settings.planner_model,
                        "hypothesis_count": len(hypotheses),
                        "elapsed_seconds": round(time.monotonic() - planner_started, 6),
                        **(
                            {"projection_manifest": planner.last_projection_manifest}
                            if planner.last_projection_manifest is not None
                            else {}
                        ),
                    },
                    state=RunState.PLAN.value,
                )
    _write_json(
        context.record.run_dir / "hypotheses.json",
        [hypothesis.model_dump(mode="json") for hypothesis in hypotheses],
    )
    context.values["hypotheses"] = hypotheses
    context.values["planner_source"] = planner_source
    return StateOutcome(
        RunState.SOLVE,
        {"hypotheses": len(hypotheses), "planner_source": planner_source},
    )
