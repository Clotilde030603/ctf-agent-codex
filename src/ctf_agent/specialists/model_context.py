"""Model specialist checkpoint identity, prompt context, and scoped session setup."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ctf_agent.auth_broker import AuthSessionHandle, ResumeAuthSessionUnavailableError
from ctf_agent.evidence.sanitizer import SecretSanitizer
from ctf_agent.ingestion.session import ScopedAsyncSession, SessionConfig
from ctf_agent.lanes import LaneCheckpoint, LaneModelIdentity, content_identity
from ctf_agent.schemas import Hypothesis
from ctf_agent.scope import HostScope

if TYPE_CHECKING:
    from ctf_agent.specialists.model import ModelSolverSpecialist


def checkpoint_seed(
    specialist: ModelSolverSpecialist,
    run_id: str,
    lane_id: str,
    hypothesis: Hypothesis,
    context: Mapping[str, object],
) -> LaneCheckpoint:
    classification = context.get("classification")
    category = (
        str(classification.get("primary_category", "misc"))
        if isinstance(classification, Mapping)
        else "misc"
    )
    skill = context.get("_skill_selection")
    skill_payload = (
        [item.model_dump(mode="json") for item in skill.identities]
        if hasattr(skill, "identities")
        else []
    )
    challenge = context.get("challenge")
    attachment_payload = {
        "challenge": challenge,
        "files": context.get("files", []),
        "triage": context.get("triage", {}),
    }
    sanitizer = SecretSanitizer()
    safe_hypothesis = sanitizer.sanitize(hypothesis.claim).text
    return LaneCheckpoint(
        lane_id=lane_id,
        run_id=run_id,
        hypothesis_id=hypothesis.id,
        hypothesis_revision=content_identity(hypothesis.model_dump(mode="json")),
        category=category,
        model_identity=LaneModelIdentity(
            specialist=specialist.name,
            model=specialist.settings.solver_model,
            effort=specialist.settings.solver_effort,
            skill_sha256=content_identity(skill_payload),
            capability_sha256=content_identity(context.get("runtime_capabilities", {})),
            attachment_sha256=content_identity(attachment_payload),
        ),
        hypothesis=safe_hypothesis,
        restatement=safe_hypothesis,
    )


def task() -> str:
    return (
        "Solve the hypothesis in the labeled untrusted context through controlled actions. "
        "Challenge files are copied under files/. Use only argv command actions or "
        "relative write_file actions. Create a data-dependent solve.py, execute it, "
        "record new facts with provenance, and finish only after emitting any flag "
        "candidates with source_artifact, source_location, derivation, and solver_command."
    )


def worker_context(
    hypothesis: Hypothesis,
    context: Mapping[str, object],
    lane_dir: Path,
) -> dict[str, Any]:
    return {
        "hypothesis": hypothesis.model_dump(mode="json"),
        "challenge": context.get("challenge", {}),
        "flag_policy": context.get("flag_policy", {}),
        "classification": context.get("classification", {}),
        "triage": context.get("triage", {}),
        "previous_attempts_and_failures": context.get(
            "previous_attempts_and_failures", []
        ),
        "preflight_results": context.get("preflight_results", []),
        "lane_workspace": str(lane_dir),
        "challenge_copy": "files/",
        "network_policy": (
            "Docker commands use --network=none. Remote access is available only "
            "through the structured http_request action and only for authorized hosts."
        ),
        "authorized_service_hosts": context.get("service_hosts", []),
        "runtime_capabilities": context.get("runtime_capabilities", {}),
        "_skill_selection": context.get("_skill_selection"),
    }


def http_session(
    specialist: ModelSolverSpecialist,
    context: Mapping[str, object],
) -> ScopedAsyncSession | None:
    challenge = context.get("challenge")
    if not isinstance(challenge, Mapping):
        return None
    challenge_url = challenge.get("url")
    if not isinstance(challenge_url, str) or not challenge_url:
        return None
    raw_hosts = context.get("service_hosts", [])
    extra_hosts = [str(item) for item in raw_hosts] if isinstance(raw_hosts, list) else []
    scope = HostScope.from_url(
        challenge_url,
        extra_hosts=extra_hosts,
        allow_private_hosts=specialist.settings.allow_private_hosts,
    )
    handle = context.get("auth_handle")
    if context.get("auth_session_status") == "unavailable":
        raise ResumeAuthSessionUnavailableError
    if specialist.auth_broker is not None and isinstance(handle, AuthSessionHandle):
        return specialist.auth_broker.clone_lane(handle, scope)
    return ScopedAsyncSession(
        scope,
        config=SessionConfig(
            timeout_seconds=specialist.settings.request_timeout_seconds,
            retry_budget=specialist.settings.retry_budget,
            rate_limit_per_second=specialist.settings.rate_limit_per_second,
        ),
    )
