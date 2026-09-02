"""Extracted workflow behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ctf_agent.workflow import AutonomousWorkflow

import hashlib

from ctf_agent.engine import RunContext, StateOutcome
from ctf_agent.schemas import (
    RunState,
    SubmissionVerdict,
)
from ctf_agent.verification import (
    FlagGate,
    SubmissionBudget,
)
from ctf_agent.workflow_parts.io import _write_json


async def submit(workflow: AutonomousWorkflow, context: RunContext) -> StateOutcome:
    candidate = workflow._candidate(context)
    independently_approved = candidate.independent_verified or (
        workflow.settings.backend == "static" and workflow.settings.approve_static_submission
    )
    submission_allowed = all(
        (
            candidate.format_match,
            candidate.provenance_verified,
            candidate.replay_verified,
            candidate.data_dependency_verified,
            independently_approved,
        )
    )
    candidate = candidate.model_copy(update={"submission_allowed": submission_allowed})
    context.values["candidate"] = candidate
    required_checks = {
        "format_match": candidate.format_match,
        "provenance_verified": candidate.provenance_verified,
        "replay_verified": candidate.replay_verified,
        "data_dependency_verified": candidate.data_dependency_verified,
        "independent_or_manually_approved": independently_approved,
        "submission_allowed": candidate.submission_allowed,
    }
    failed_checks = [name for name, passed in required_checks.items() if not passed]
    if not context.record.auto_submit:
        output = context.record.run_dir / "verified-candidate.json"
        _write_json(
            output,
            {
                "status": "verified_not_submitted",
                "candidate": candidate.model_dump(mode="json"),
                "instructions": "review this private run artifact before manual submission",
            },
        )
        context.ledger.append(
            context.record.run_id,
            "flag.ready",
            {
                "candidate_path": str(output.relative_to(context.record.run_dir)),
                **required_checks,
            },
            state=RunState.READY.value,
        )
        return StateOutcome(
            RunState.READY,
            {
                "verified": True,
                "submitted": False,
                "candidate_path": str(output),
                "submission_blockers": failed_checks,
            },
        )
    if failed_checks:
        raise RuntimeError(
            "submission blocked by incomplete verification: " + ", ".join(failed_checks)
        )
    previous_verdict = context.store.latest_submission_verdict(
        context.record.run_id, candidate.value
    )
    if previous_verdict in {
        SubmissionVerdict.ACCEPTED.value,
        SubmissionVerdict.ALREADY_SOLVED.value,
    }:
        return StateOutcome(
            RunState.EVIDENCE_PENDING,
            {"accepted": True, "flag": candidate.value, "resumed": True},
        )
    if previous_verdict == SubmissionVerdict.WRONG.value:
        context.store.reject_candidate(
            context.record.run_id,
            candidate.value,
            "previous durable submission verdict was wrong",
        )
        wrong_count = context.store.submission_count_for_verdict(
            context.record.run_id, SubmissionVerdict.WRONG.value
        )
        target = RunState.TRIAGE if wrong_count >= 2 else RunState.PLAN
        return StateOutcome(
            target,
            {"accepted": False, "wrong_count": wrong_count, "resumed": True},
        )
    adapter = await workflow._adapter(context)
    pending = context.store.pending_submission(context.record.run_id)
    if pending is not None:
        attempt_id, pending_value = pending
        if pending_value != candidate.value:
            raise RuntimeError("pending submission does not match verified candidate")
        resolver = getattr(adapter, "resolve_submission", None)
        if resolver is None:
            raise RuntimeError("pending submission cannot be resolved by this platform")
        result = await resolver(workflow._challenge(context), candidate.value)
        if result is None or result.verdict in {
            SubmissionVerdict.UNKNOWN,
            SubmissionVerdict.RATE_LIMITED,
        }:
            raise RuntimeError(
                "pending submission outcome is unknown; refusing duplicate submission"
            )
        context.ledger.append(
            context.record.run_id,
            "submission.resolved",
            {"attempt_id": attempt_id, "verdict": result.verdict.value},
            state=RunState.SUBMIT.value,
        )
    else:
        used = context.store.submission_count(context.record.run_id)
        budget = SubmissionBudget(workflow.settings.submission_budget, used)
        gate = FlagGate(workflow._challenge(context).flag_policy)
        decision = gate.evaluate(candidate, budget)
        if not gate.reserve_submission(decision, budget):
            raise RuntimeError(f"submission blocked: {decision.reason}")
        attempt_id = hashlib.sha256(
            f"{context.record.run_id}\0{candidate.value}\0{used}".encode()
        ).hexdigest()
        context.store.begin_submission(context.record.run_id, candidate.value, attempt_id)
        context.ledger.append(
            context.record.run_id,
            "submission.pending",
            {"attempt_id": attempt_id, "flag": candidate.value},
            state=RunState.SUBMIT.value,
        )
        result = await adapter.submit_flag(workflow._challenge(context), candidate.value)
    if result.verdict is SubmissionVerdict.AUTH_REQUIRED:
        context.store.abandon_submission(attempt_id, result.verdict.value)
        context.ledger.append(
            context.record.run_id,
            "authentication.expired",
            {"attempt_id": attempt_id, "message": result.message},
            state=RunState.SUBMIT.value,
        )
        return StateOutcome(
            RunState.AUTHENTICATE,
            {"authenticated": False, "submission_consumed": False},
        )
    context.store.record_submission(
        context.record.run_id,
        candidate.value,
        result.verdict.value,
        attempt_id=attempt_id,
    )
    context.values["submission"] = result
    context.ledger.append(
        context.record.run_id,
        "flag.submitted",
        {
            "accepted": result.verdict
            in {SubmissionVerdict.ACCEPTED, SubmissionVerdict.ALREADY_SOLVED},
            "flag": candidate.value,
            "verdict": result.verdict.value,
            "message": result.message,
        },
        state=RunState.SUBMIT.value,
    )
    if result.verdict in {SubmissionVerdict.ACCEPTED, SubmissionVerdict.ALREADY_SOLVED}:
        return StateOutcome(
            RunState.EVIDENCE_PENDING,
            {"accepted": True, "flag": candidate.value},
        )
    if result.verdict is SubmissionVerdict.WRONG:
        context.store.reject_candidate(context.record.run_id, candidate.value, result.message)
        wrong_count = context.store.submission_count_for_verdict(
            context.record.run_id, SubmissionVerdict.WRONG.value
        )
        target = RunState.TRIAGE if wrong_count >= 2 else RunState.PLAN
        return StateOutcome(target, {"accepted": False, "wrong_count": wrong_count})
    raise RuntimeError(f"submission did not produce a final verdict: {result.verdict.value}")
