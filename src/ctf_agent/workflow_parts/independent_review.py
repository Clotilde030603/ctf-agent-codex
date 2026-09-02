"""Independent model review for verified solver candidates."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ctf_agent.budget_types import (
    BudgetExhaustedError,
    BudgetPurpose,
    BudgetRequest,
    BudgetRequestId,
    BudgetRole,
)
from ctf_agent.engine import RunContext
from ctf_agent.schemas import Challenge, FlagCandidate, RunState
from ctf_agent.verification import BlindVerificationOutcome, ModelBlindReviewer

if TYPE_CHECKING:
    from ctf_agent.workflow import AutonomousWorkflow


@dataclass(frozen=True, slots=True)
class ReviewInput:
    challenge: Challenge
    candidate: FlagCandidate
    blind: BlindVerificationOutcome


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    accepted: bool
    independently_verified: bool
    reason: str


async def review_candidate(
    workflow: AutonomousWorkflow,
    context: RunContext,
    review_input: ReviewInput,
) -> ReviewDecision:
    if workflow.settings.backend != "codex":
        return ReviewDecision(True, False, "no independent reviewer")
    budget = workflow._model_budget(context)
    request_index = budget.snapshot().requested + 1
    request = BudgetRequest(
        BudgetRole.VERIFIER,
        BudgetPurpose.VERIFY,
        BudgetRequestId(f"{context.record.run_id}:verifier:{request_index}"),
    )
    try:
        lease = await budget.acquire(request)
    except BudgetExhaustedError:
        return ReviewDecision(False, False, "reviewer model call budget exhausted")
    await budget.start(lease.lease_id)
    context.ledger.append(
        context.record.run_id,
        "model.request",
        {
            "role": "verifier",
            "purpose": BudgetPurpose.VERIFY.value,
            "request_id": lease.request_id,
            "model": workflow.settings.verifier_model,
            "request_index": request_index,
        },
        state=RunState.VERIFY.value,
    )
    started = time.monotonic()
    review = await ModelBlindReviewer(
        workflow.settings,
        context.record.run_dir,
        review_input.challenge.flag_policy.model_dump(mode="json"),
        backend_factory=workflow._reviewer_backend_factory,
        skills=workflow._skill_selection(context, review_input.challenge.category),
    ).derive()
    await budget.commit(lease.lease_id)
    provenance = review_input.blind.provenance
    expected_source = (
        str(provenance.artifact_path.relative_to(context.record.run_dir))
        if provenance and provenance.artifact_path
        else ""
    )
    matching_findings = [
        finding
        for finding in review.findings
        if finding.candidate == review_input.candidate.value
        and finding.source_artifact == expected_source
    ]
    elapsed = round(time.monotonic() - started, 6)
    if not review.accepted or not matching_findings:
        context.ledger.append(
            context.record.run_id,
            "model.failure",
            {
                "role": "verifier",
                "message": review.reason,
                "derived_candidate_count": len(review.derived_candidates),
                "elapsed_seconds": elapsed,
            },
            state=RunState.VERIFY.value,
        )
        return ReviewDecision(False, False, f"reviewer: {review.reason}")
    context.ledger.append(
        context.record.run_id,
        "model.completed",
        {
            "role": "verifier",
            "model": workflow.settings.verifier_model,
            "derived_candidate_count": len(review.derived_candidates),
            "elapsed_seconds": elapsed,
        },
        state=RunState.VERIFY.value,
    )
    return ReviewDecision(True, True, review.reason)
