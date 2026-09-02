"""Non-borrowable context contracts selected before optional projection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ctf_agent.context_projector.models import ProjectedSection, TrustLabel


def _mandatory_sections(context: Any) -> tuple[ProjectedSection, ...]:
    mapping = context if isinstance(context, Mapping) else {}
    challenge = mapping.get("challenge", {})
    challenge_map = challenge if isinstance(challenge, Mapping) else {}
    hosts = mapping.get(
        "authorized_service_hosts",
        mapping.get("service_hosts", challenge_map.get("service_hosts", [])),
    )
    scope = {"challenge_url": challenge_map.get("url"), "authorized_hosts": hosts}
    lane = mapping.get("lane_checkpoint")
    lane_map = lane if isinstance(lane, Mapping) else {}
    active = mapping.get(
        "hypothesis",
        {
            str(key): value
            for key, value in lane_map.items()
            if str(key) not in {"facts", "verified_facts"}
        }
        or {"state": "unspecified"},
    )
    flag_policy = mapping.get("flag_policy", challenge_map.get("flag_policy", {}))
    sections = [
        ProjectedSection(
            section_id="safety",
            trust_label=TrustLabel.TRUSTED_POLICY,
            provenance="context_projector.policy",
            mandatory=True,
            content={"authorized_ctf_only": True, "structured_actions_only": True},
        ),
        ProjectedSection(
            section_id="scope",
            trust_label=TrustLabel.TRUSTED_RUNTIME,
            provenance="request.context.challenge/service_hosts",
            mandatory=True,
            content=scope,
        ),
        ProjectedSection(
            section_id="auth_redaction",
            trust_label=TrustLabel.TRUSTED_POLICY,
            provenance="context_projector.policy",
            mandatory=True,
            content={"credentials_available_to_model": False, "credential_marker": "REDACTED"},
        ),
        ProjectedSection(
            section_id="active_hypothesis_lane",
            trust_label=TrustLabel.UNTRUSTED_DATA,
            provenance="request.context.hypothesis_or_lane_checkpoint",
            mandatory=True,
            content=active,
        ),
        ProjectedSection(
            section_id="flag_policy",
            trust_label=TrustLabel.TRUSTED_RUNTIME,
            provenance="request.context.flag_policy",
            mandatory=True,
            content=flag_policy,
        ),
    ]
    if "challenge" in mapping:
        sections.append(
            ProjectedSection(
                section_id="challenge",
                trust_label=TrustLabel.TRUSTED_RUNTIME,
                provenance="request.context.challenge",
                mandatory=True,
                content=challenge,
            )
        )
    validated_facts = _validated_facts(lane_map.get("facts"))
    if validated_facts:
        sections.append(
            ProjectedSection(
                section_id="verified_facts",
                trust_label=TrustLabel.TRUSTED_RUNTIME,
                provenance="request.context.lane_checkpoint.facts.controller_validated",
                mandatory=True,
                content=validated_facts,
            )
        )
    if "candidate_evidence" in mapping:
        sections.append(
            ProjectedSection(
                section_id="candidate_evidence",
                trust_label=TrustLabel.UNTRUSTED_DATA,
                provenance="request.context.candidate_evidence",
                mandatory=True,
                content=mapping.get("candidate_evidence"),
            )
        )
    return tuple(sections)


def _validated_facts(value: Any) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    return [
        item
        for item in value
        if isinstance(item, Mapping)
        and item.get("status") == "validated"
        and item.get("source") in {"command", "artifact"}
    ]
