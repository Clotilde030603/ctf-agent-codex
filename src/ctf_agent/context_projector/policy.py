"""Deterministic role policy, compression, and redaction helpers."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from ctf_agent.context_projector.models import (
    ProjectedSection,
    ProjectionAction,
    ProjectionSection,
    TrustLabel,
)
from ctf_agent.security import redact_url

_TRUNCATED: Final = "deterministic_truncation_v1"
_SECRET_KEY: Final = re.compile(
    r"(?:^|[_-])(?:authorization|cookie|token|api[_-]?key|secret|password|passwd|session|csrf|credential|jwt)(?:$|[_-])",
    re.IGNORECASE,
)
_BEARER: Final = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT: Final = re.compile(
    r"(?i)\b(?:api[_-]?key|token|secret|password|session|csrf)\s*[:=]\s*[^\s,;]+"
)
_PRIORITIES: Final[dict[str, tuple[str, ...]]] = {
    "planner": (
        "challenge",
        "flag_policy",
        "classification",
        "files",
        "previous_attempts_and_failures",
        "runtime_capabilities",
    ),
    "replan": (
        "previous_attempts_and_failures",
        "challenge",
        "classification",
        "files",
        "runtime_capabilities",
    ),
    "solver": (
        "challenge",
        "flag_policy",
        "triage",
        "preflight_results",
        "recent_reports",
        "runtime_capabilities",
    ),
    "worker": (
        "challenge",
        "flag_policy",
        "triage",
        "preflight_results",
        "recent_reports",
        "runtime_capabilities",
    ),
    "verifier": ("flag_policy", "solver", "files", "evidence", "runtime_capabilities"),
    "reviewer": ("flag_policy", "solver", "files", "evidence", "runtime_capabilities"),
}
_QUOTAS: Final[dict[str, int]] = {
    "planner": 65_536,
    "replan": 73_728,
    "solver": 81_920,
    "worker": 81_920,
    "verifier": 49_152,
    "reviewer": 49_152,
}


def _original_sections(context: Any) -> tuple[ProjectedSection, ...]:
    mapping = context if isinstance(context, Mapping) else {}
    return tuple(
        ProjectedSection(
            section_id=str(key),
            trust_label=TrustLabel.UNTRUSTED_DATA,
            provenance=f"request.context.{key}",
            mandatory=False,
            content=value,
        )
        for key, value in sorted(mapping.items(), key=lambda item: str(item[0]))
    )


def _ordered_keys(role: str, context: Any) -> tuple[str, ...]:
    mapping = context if isinstance(context, Mapping) else {}
    excluded = {
        "hypothesis",
        "challenge",
        "authorized_service_hosts",
        "service_hosts",
        "flag_policy",
        "candidate_evidence",
    }
    available = {str(key) for key in mapping if str(key) not in excluded}
    priority = _PRIORITIES.get(role, _PRIORITIES["solver"])
    return tuple(key for key in priority if key in available) + tuple(
        sorted(available - set(priority))
    )


def _summary_section(section: ProjectedSection, value: Any) -> ProjectedSection:
    return section.model_copy(
        update={
            "content": {
                section.section_id: {
                    "summary": _shape(value),
                    "sha256": hashlib.sha256(_canonical(value)).hexdigest(),
                }
            },
            "truncation_marker": _TRUNCATED,
        }
    )


def _decision(
    order: int,
    section: ProjectedSection,
    action: ProjectionAction,
    original_bytes: int | None = None,
    item_count: int | None = None,
) -> ProjectionSection:
    final_bytes = len(_canonical(section.content))
    return ProjectionSection(
        order=order,
        section_id=section.section_id,
        action=action,
        mandatory=section.mandatory,
        trust_label=section.trust_label,
        provenance=section.provenance,
        original_bytes=final_bytes if original_bytes is None else original_bytes,
        final_bytes=final_bytes,
        item_count=_count(section.content) if item_count is None else item_count,
        truncation_marker=section.truncation_marker,
    )


def _bounded(value: Any, max_items: int) -> tuple[Any, int, bool]:
    if isinstance(value, Mapping):
        ordered = sorted(value.items(), key=lambda item: str(item[0]))
        kept = ordered[:max_items]
        output = {str(key): _bounded(item, max_items)[0] for key, item in kept}
        return (
            output,
            len(ordered),
            len(kept) != len(ordered) or any(_bounded(item, max_items)[2] for _, item in kept),
        )
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        kept = list(value[:max_items])
        nested = [_bounded(item, max_items) for item in kept]
        return (
            [item[0] for item in nested],
            len(value),
            len(kept) != len(value) or any(item[2] for item in nested),
        )
    return value, 1, False


def _redact(value: Any, key: str = "") -> Any:
    if _SECRET_KEY.search(key):
        return "REDACTED"
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact(item, str(item_key))
            for item_key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, list | tuple):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_text(value: str) -> str:
    return _SECRET_ASSIGNMENT.sub("REDACTED", _BEARER.sub("REDACTED", redact_url(value)))


def _shape(value: Any) -> str:
    if isinstance(value, Mapping):
        return f"mapping:{len(value)}"
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return f"sequence:{len(value)}"
    if isinstance(value, str):
        return f"text_bytes:{len(value.encode())}"
    return type(value).__name__


def _count(value: Any) -> int:
    if isinstance(value, Mapping | Sequence) and not isinstance(value, str | bytes | bytearray):
        return len(value)
    return 1


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str
    ).encode()


def _normalized_role(role: str | None) -> str:
    return role.strip().lower() if role and role.strip() else "solver"
