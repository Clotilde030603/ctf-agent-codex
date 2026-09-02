"""Extracted workflow behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ctf_agent.workflow import AutonomousWorkflow

import hashlib

from ctf_agent.engine import RunContext, StateOutcome
from ctf_agent.schemas import (
    RunState,
    VerifiedCandidateRecord,
)
from ctf_agent.verification import BlindVerifier, FlagGate, RejectedCandidates, ReplayVerifier
from ctf_agent.workflow_parts.independent_review import ReviewInput, review_candidate
from ctf_agent.workflow_parts.io import _sha256_file


async def verify(workflow: AutonomousWorkflow, context: RunContext) -> StateOutcome:
    challenge = workflow._challenge(context)
    results = workflow._specialist_results(context)
    rejected = RejectedCandidates()
    gate = FlagGate(challenge.flag_policy, rejected)
    reasons: list[str] = []
    for result in results:
        for candidate in result.flag_candidates:
            context.ledger.append(
                context.record.run_id,
                "flag.candidate",
                {
                    "hypothesis_id": result.hypothesis_id,
                    "candidate_sha256": hashlib.sha256(candidate.value.encode()).hexdigest(),
                    "source_artifact": candidate.source_artifact,
                    "source_location": candidate.source_location,
                    "confidence": candidate.confidence,
                },
                state=RunState.VERIFY.value,
                idempotency_key=(
                    "flag-candidate:" + hashlib.sha256(candidate.value.encode()).hexdigest()
                ),
            )
            if context.store.is_rejected(context.record.run_id, candidate.value):
                rejected.add(candidate.value)
            verifier = ReplayVerifier(
                gate,
                context.record.run_dir / "solve.py",
                flag_regex=challenge.flag_policy.pattern,
                timeout_seconds=workflow.settings.tool_timeout_seconds,
            )
            outcome = verifier.verify(candidate)
            replay_result = outcome.replay
            context.ledger.append(
                context.record.run_id,
                "solver.replayed",
                {
                    "accepted": outcome.accepted,
                    "returncode": replay_result.returncode if replay_result else None,
                    "matched_candidate": bool(
                        replay_result and replay_result.matched_flag is not None
                    ),
                },
                state=RunState.VERIFY.value,
            )
            if not outcome.accepted:
                reason = f"replay: {outcome.reason}"
                context.store.reject_candidate(context.record.run_id, candidate.value, reason)
                reasons.append(f"{candidate.value}: {reason}")
                continue
            blind = BlindVerifier(
                context.record.run_dir,
                challenge.flag_policy,
                solver_path=context.record.run_dir / "solve.py",
                timeout_seconds=workflow.settings.tool_timeout_seconds,
            ).verify(candidate)
            if not blind.accepted:
                reason = f"{blind.failure_stage}: {blind.reason}"
                context.store.reject_candidate(context.record.run_id, candidate.value, reason)
                reasons.append(f"{candidate.value}: {reason}")
                continue
            review = await review_candidate(
                workflow,
                context,
                ReviewInput(challenge, candidate, blind),
            )
            if not review.accepted:
                reasons.append(f"{candidate.value}: {review.reason}")
                continue
            reviewer_reason = review.reason
            independent_verified = review.independently_verified
            context.ledger.append(
                context.record.run_id,
                "independent.verified",
                {
                    "accepted": independent_verified,
                    "backend": workflow.settings.backend,
                    "reason": reviewer_reason,
                },
                state=RunState.VERIFY.value,
            )
            data_dependency_verified = bool(
                blind.negative_control and blind.negative_control.matched_flag != candidate.value
            )
            provenance_verified = bool(blind.provenance and blind.provenance.accepted)
            submission_allowed = all(
                (
                    provenance_verified,
                    data_dependency_verified,
                    independent_verified
                    or (
                        workflow.settings.backend == "static"
                        and workflow.settings.approve_static_submission
                    ),
                )
            )
            verified = candidate.model_copy(
                update={
                    "format_match": True,
                    "provenance_verified": provenance_verified,
                    "replay_verified": True,
                    "data_dependency_verified": data_dependency_verified,
                    "independent_verified": independent_verified,
                    "submission_allowed": submission_allowed,
                }
            )
            assert blind.provenance is not None
            assert blind.provenance.artifact_path is not None
            source_artifact = blind.provenance.artifact_path
            verification_record = VerifiedCandidateRecord(
                run_id=context.record.run_id,
                candidate=verified,
                solver_sha256=_sha256_file(context.record.run_dir / "solve.py"),
                source_artifact=str(source_artifact.relative_to(context.record.run_dir)),
                source_artifact_sha256=_sha256_file(source_artifact),
            )
            context.store.save_verified_candidate(verification_record)
            context.values["candidate"] = verified
            context.values["replay"] = outcome.replay
            context.ledger.append(
                context.record.run_id,
                "flag.verified",
                {
                    "accepted": True,
                    "flag": verified.value,
                    "reason": f"{blind.reason}; {reviewer_reason}",
                    "format_match": verified.format_match,
                    "provenance_verified": verified.provenance_verified,
                    "replay_verified": verified.replay_verified,
                    "data_dependency_verified": verified.data_dependency_verified,
                    "independent_verified": verified.independent_verified,
                    "submission_allowed": verified.submission_allowed,
                },
                state=RunState.VERIFY.value,
            )
            return StateOutcome(
                RunState.REPRODUCE,
                {"accepted": True, "flag": verified.value},
            )
    context.ledger.append(
        context.record.run_id,
        "flag.verification_failed",
        {"reasons": reasons},
        state=RunState.VERIFY.value,
    )
    for reason in reasons:
        context.ledger.append(
            context.record.run_id,
            "flag.rejected",
            {"reason": reason},
            state=RunState.VERIFY.value,
        )
    return StateOutcome(
        RunState.SOLVE if context.values.get("adaptive_frontier") is True else RunState.PLAN,
        {"accepted": False, "reasons": reasons},
    )
