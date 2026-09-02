"""Extracted workflow behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ctf_agent.workflow import AutonomousWorkflow


from ctf_agent.engine import RunContext
from ctf_agent.schemas import (
    Challenge,
    FlagCandidate,
    SpecialistResult,
    VerifiedCandidateRecord,
)
from ctf_agent.workflow_parts.io import _sha256_file


def _challenge(workflow: AutonomousWorkflow, context: RunContext) -> Challenge:
    value = context.values.get("challenge")
    if isinstance(value, Challenge):
        return value
    challenge = Challenge.model_validate(
        workflow._load_json(context.record.run_dir / "challenge.json")
    )
    context.values["challenge"] = challenge
    return challenge


def _specialist_results(
    workflow: AutonomousWorkflow, context: RunContext
) -> list[SpecialistResult]:
    values = context.values.get("specialist_results")
    if isinstance(values, list) and all(isinstance(item, SpecialistResult) for item in values):
        return values
    payload = workflow._load_json(context.record.run_dir / "artifacts" / "specialist-results.json")
    results = [SpecialistResult.model_validate(item) for item in payload]
    context.values["specialist_results"] = results
    return results


def _candidate(
    workflow: AutonomousWorkflow,
    context: RunContext,
    *,
    allow_legacy_accepted: bool = False,
) -> FlagCandidate:
    record = context.store.load_verified_candidate(context.record.run_id)
    if record is not None:
        if not record.valid:
            legacy_evidence_only = (
                allow_legacy_accepted
                and record.invalidation_reason
                == "legacy verification has no original integrity hashes"
                and context.store.has_accepted_submission(context.record.run_id)
            )
            if not legacy_evidence_only:
                raise RuntimeError(
                    "verified candidate was invalidated: "
                    + (record.invalidation_reason or "unknown reason")
                )
        solver = context.record.run_dir / "solve.py"
        source = context.record.run_dir / record.source_artifact
        mismatch = None
        if not solver.is_file() or _sha256_file(solver) != record.solver_sha256:
            mismatch = "solver SHA-256 changed after verification"
        elif not source.is_file() or _sha256_file(source) != record.source_artifact_sha256:
            mismatch = "provenance artifact SHA-256 changed after verification"
        if mismatch is not None:
            context.store.invalidate_verified_candidate(context.record.run_id, mismatch)
            raise RuntimeError(mismatch)
        context.values["candidate"] = record.candidate
        return record.candidate

    # Backward-compatible migration for pre-verification-table runs. Only
    # explicit booleans in the historical event are restored; missing values
    # remain False and are never promoted to success.
    verified_events = [
        item
        for item in context.ledger.list(context.record.run_id)
        if item["event_type"] == "flag.verified" and item["payload"].get("flag")
    ]
    if not verified_events:
        raise RuntimeError("resume data has no verified flag candidate")
    payload = verified_events[-1]["payload"]
    flag = str(payload["flag"])
    for result in workflow._specialist_results(context):
        for candidate in result.flag_candidates:
            if candidate.value == flag:
                verified = candidate.model_copy(
                    update={
                        "format_match": payload.get("format_match") is True,
                        "provenance_verified": payload.get("provenance_verified") is True,
                        "replay_verified": payload.get("replay_verified") is True,
                        "data_dependency_verified": payload.get("data_dependency_verified") is True,
                        "independent_verified": payload.get("independent_verified") is True,
                        "submission_allowed": False,
                    }
                )
                source = (context.record.run_dir / candidate.source_artifact).resolve()
                if context.record.run_dir.resolve() not in source.parents or not source.is_file():
                    raise RuntimeError(
                        "legacy verified candidate has no hashable provenance artifact"
                    )
                migrated = VerifiedCandidateRecord(
                    run_id=context.record.run_id,
                    candidate=verified,
                    solver_sha256=_sha256_file(context.record.run_dir / "solve.py"),
                    source_artifact=str(source.relative_to(context.record.run_dir)),
                    source_artifact_sha256=_sha256_file(source),
                    valid=False,
                    invalidation_reason=("legacy verification has no original integrity hashes"),
                )
                context.store.save_verified_candidate(migrated)
                if not (
                    allow_legacy_accepted
                    and context.store.has_accepted_submission(context.record.run_id)
                ):
                    raise RuntimeError(
                        "legacy verification requires fresh hash-backed re-verification"
                    )
                context.values["candidate"] = verified
                return verified
    raise RuntimeError("verified candidate is absent from specialist artifacts")
