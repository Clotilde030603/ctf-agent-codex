"""Operational benchmark metrics derived from scorer-owned workflow events."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def operational_event_metrics(
    events: Iterable[Mapping[str, Any]],
) -> dict[str, int]:
    """Count feature, recovery, frontier, and context observations."""
    values = {
        "context_bytes": 0,
        "model_starvation_count": 0,
        "repeated_action_count": 0,
        "lane_retirement_count": 0,
        "lane_replacement_count": 0,
        "tcp_connect_count": 0,
        "restart_count": 0,
        "recovery_count": 0,
        "checkpoint_count": 0,
        "checkpoint_resume_count": 0,
        "budget_extension_count": 0,
        "projection_count": 0,
        "frontier_event_count": 0,
        "available_capability_count": 0,
        "elastic_budget_observed": 0,
        "lane_continuity_observed": 0,
        "adaptive_frontier_observed": 0,
    }
    command_fingerprints: set[str] = set()
    for event in events:
        event_type = str(
            event.get("type") or event.get("event_type") or event.get("stage") or ""
        )
        raw_payload = event.get("payload") or event.get("data") or {}
        payload = raw_payload if isinstance(raw_payload, Mapping) else {}
        fingerprint = payload.get("fingerprint")
        if event_type == "worker.command" and isinstance(fingerprint, str):
            if fingerprint in command_fingerprints:
                values["repeated_action_count"] += 1
            command_fingerprints.add(fingerprint)
        projection = payload.get("projection_manifest")
        if isinstance(projection, Mapping):
            _record_projection(values, projection)
        if event_type == "context.projection_completed":
            _record_projection(values, payload)
        if event_type == "budget.denied":
            values["model_starvation_count"] += 1
        if event_type == "budget.extended":
            values["budget_extension_count"] += 1
        if event_type == "lane.retired":
            values["lane_retirement_count"] += 1
        if event_type == "lane.replaced":
            values["lane_replacement_count"] += 1
        if event_type.startswith("frontier.") or event_type.startswith("lane."):
            values["frontier_event_count"] += 1
        if event_type == "worker.tcp_connect":
            values["tcp_connect_count"] += 1
        if event_type == "run.resumed":
            values["restart_count"] += 1
        if event_type in {"budget.recovered", "lane.checkpoint.reset"}:
            values["recovery_count"] += 1
        if event_type == "benchmark.runtime_observed":
            _record_runtime(values, payload)
    return values


def _record_projection(
    values: dict[str, int], payload: Mapping[str, Any]
) -> None:
    final_bytes = payload.get("final_bytes")
    if isinstance(final_bytes, int) and final_bytes >= 0:
        values["context_bytes"] += final_bytes
        values["projection_count"] += 1


def _record_runtime(values: dict[str, int], payload: Mapping[str, Any]) -> None:
    available = payload.get("available_capabilities")
    checkpoints = payload.get("checkpoint_count")
    resumes = payload.get("checkpoint_resumes")
    values["available_capability_count"] = available if isinstance(available, int) else 0
    values["checkpoint_count"] = checkpoints if isinstance(checkpoints, int) else 0
    values["checkpoint_resume_count"] = resumes if isinstance(resumes, int) else 0
    values["elastic_budget_observed"] = int(payload.get("elastic_budget") is True)
    values["lane_continuity_observed"] = int(payload.get("lane_continuity") is True)
    values["adaptive_frontier_observed"] = int(payload.get("adaptive_frontier") is True)
