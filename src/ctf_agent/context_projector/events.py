"""Sanitized deterministic event payloads for projection decisions."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ctf_agent.context_projector.models import ProjectionManifest


def projection_item_events(manifest: ProjectionManifest) -> tuple[dict[str, Any], ...]:
    """Describe every projection decision without carrying section content."""
    events: list[dict[str, Any]] = []
    for section in manifest.sections:
        payload = {
            "section_id": section.section_id,
            "action": section.action.value,
            "original_bytes": section.original_bytes,
            "final_bytes": section.final_bytes,
            "provenance": section.provenance,
            "trust_label": section.trust_label.value,
            "truncation_marker": section.truncation_marker,
        }
        payload["sha256"] = hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        events.append(payload)
    return tuple(events)
