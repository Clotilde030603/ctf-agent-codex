"""Scorer metric loading and event-stream derivation."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from ctf_agent.benchmark_event_metrics import operational_event_metrics
from ctf_agent.benchmark_manifest import BenchmarkChallenge
from ctf_agent.benchmark_models import BenchmarkMetrics


def _load_metrics(run_dir: Path, challenge: BenchmarkChallenge) -> BenchmarkMetrics:
    metrics = BenchmarkMetrics()
    metrics_path = run_dir / challenge.metrics_file
    if metrics_path.is_file():
        payload = json.loads(metrics_path.read_text(encoding="utf-8"))
        if isinstance(payload, Mapping):
            metrics = _merge_metrics(metrics, payload)
            events = payload.get("events")
            if isinstance(events, list):
                metrics = _merge_metrics(metrics, _derive_event_metrics(events))
    events_path = run_dir / challenge.events_file
    if events_path.is_file():
        metrics = _merge_metrics(metrics, _derive_jsonl_metrics(events_path))
    return metrics


def _merge_metrics(
    metrics: BenchmarkMetrics,
    payload: Mapping[str, Any],
) -> BenchmarkMetrics:
    data = metrics.model_dump()
    for key in data:
        if key in payload and payload[key] is not None:
            data[key] = payload[key]
    return BenchmarkMetrics.model_validate(data)


def _derive_jsonl_metrics(path: Path) -> Mapping[str, Any]:
    events: list[Mapping[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, Mapping):
                events.append(value)
    return _derive_event_metrics(events)


def _derive_event_metrics(events: Iterable[Mapping[str, Any]]) -> Mapping[str, Any]:
    wrong = 0
    model_calls = 0
    tool_calls = 0
    worker_command_calls = 0
    http_request_calls = 0
    hallucinated_candidates = 0
    candidate_count = 0
    rejected_candidates = 0
    first_candidate: float | None = None
    first_verified: float | None = None
    first_accepted: float | None = None
    replay_verified: bool | None = None
    independent_verified: bool | None = None
    data_dependency_verified: bool | None = None
    evidence_completed: bool | None = None
    writeup_validated: bool | None = None
    resume_verified: bool | None = None
    resume_seen = False
    total_run_status: str | None = None
    model_cost = 0.0
    tool_cost = 0.0
    network_cost = 0.0
    usage_seen = False
    event_list = list(events)
    timestamps = [_event_timestamp(event) for event in event_list]
    origin = (
        min(value for value in timestamps if value is not None)
        if any(value is not None for value in timestamps)
        else None
    )
    for event in event_list:
        event_type = str(event.get("type") or event.get("event_type") or event.get("stage") or "")
        payload = event.get("payload") or event.get("data") or {}
        payload = payload if isinstance(payload, Mapping) else {}
        seconds = _event_seconds(event, origin)
        verdict = str(payload.get("verdict", "")).lower()
        if event_type in {"flag.submitted", "submission"} and verdict == "wrong":
            wrong += 1
        if event_type in {"model.call", "model.called", "model.request"}:
            model_calls += 1
        if event_type in {
            "tool.run",
            "tool.called",
            "worker.command",
            "worker.http_request",
            "worker.tool",
            "worker.tool_call",
        }:
            tool_calls += 1
        if event_type == "worker.command":
            worker_command_calls += 1
        if event_type == "worker.http_request":
            http_request_calls += 1
        if event_type in {"flag.candidate", "candidate.found"}:
            candidate_count += 1
            if bool(payload.get("hallucinated")):
                hallucinated_candidates += 1
            if first_candidate is None:
                first_candidate = seconds
        if event_type in {"flag.rejected", "candidate.rejected"}:
            rejected_candidates += 1
        if (
            event_type in {"flag.submitted", "submission"}
            and verdict
            in {
                "accepted",
                "already_solved",
            }
            and first_accepted is None
        ):
            first_accepted = seconds
        if event_type in {"solver.replayed", "solver.reproduced"} and isinstance(
            payload.get("accepted"), bool
        ):
            replay_verified = payload["accepted"] is True
        if event_type == "flag.verified":
            if isinstance(payload.get("replay_verified"), bool):
                replay_verified = payload["replay_verified"] is True
            if isinstance(payload.get("data_dependency_verified"), bool):
                data_dependency_verified = payload["data_dependency_verified"] is True
            if isinstance(payload.get("independent_verified"), bool):
                independent_verified = payload["independent_verified"] is True
            if payload.get("accepted") is True and first_verified is None:
                first_verified = seconds
        if event_type == "independent.verified" and isinstance(payload.get("accepted"), bool):
            independent_verified = payload["accepted"] is True
        if event_type in {"writeup.validated", "writeup"}:
            explicit = payload.get("accepted", payload.get("ok"))
            if isinstance(explicit, bool):
                writeup_validated = explicit
        if event_type in {"evidence.captured", "evidence.completed"} and isinstance(
            payload.get("accepted"), bool
        ):
            evidence_completed = payload["accepted"] is True
        if event_type == "run.resumed":
            resume_seen = True
        if event_type == "resume.verified" and payload.get("accepted") is True:
            resume_verified = True
        if event_type == "usage.scored":
            usage_seen = True
            model_value = payload.get("model_cost")
            tool_value = payload.get("tool_cost")
            network_value = payload.get("network_cost")
            if isinstance(model_value, int | float) and model_value >= 0:
                model_cost += float(model_value)
            if isinstance(tool_value, int | float) and tool_value >= 0:
                tool_cost += float(tool_value)
            if isinstance(network_value, int | float) and network_value >= 0:
                network_cost += float(network_value)
        if event_type == "state.transition":
            target = str(payload.get("to", ""))
            if target in {"DONE", "DONE_WITH_WARNINGS", "READY"}:
                total_run_status = target
                if resume_seen:
                    resume_verified = True

    if not usage_seen:
        model_cost = float(model_calls)
        tool_cost = float(tool_calls) / 2
        network_cost = float(http_request_calls) / 4

    return {
        "wrong_submissions": wrong,
        "model_calls": model_calls,
        "tool_calls": tool_calls,
        "worker_command_calls": worker_command_calls,
        "http_request_calls": http_request_calls,
        "hallucinated_candidates": hallucinated_candidates,
        "candidate_count": candidate_count,
        "rejected_candidates": rejected_candidates,
        "time_to_candidate_seconds": first_candidate,
        "time_to_verified_seconds": first_verified,
        "time_to_accepted_seconds": first_accepted,
        "replay_verified": replay_verified,
        "independent_verified": independent_verified,
        "data_dependency_verified": data_dependency_verified,
        "evidence_completed": evidence_completed,
        "writeup_validated": writeup_validated,
        "resume_verified": resume_verified,
        "total_run_status": total_run_status,
        "model_cost": model_cost,
        "tool_cost": tool_cost,
        "network_cost": network_cost,
        **operational_event_metrics(event_list),
    }


def _event_seconds(event: Mapping[str, Any], origin: datetime | None = None) -> float | None:
    for key in ("seconds", "elapsed_seconds", "time_seconds"):
        value = event.get(key)
        if isinstance(value, int | float):
            return float(value)
    payload = event.get("payload") or event.get("data") or {}
    if isinstance(payload, Mapping):
        value = payload.get("seconds") or payload.get("elapsed_seconds")
        if isinstance(value, int | float):
            return float(value)
    timestamp = _event_timestamp(event)
    if timestamp is not None and origin is not None:
        return max(0.0, (timestamp - origin).total_seconds())
    return None


def _event_timestamp(event: Mapping[str, Any]) -> datetime | None:
    value = event.get("created_at") or event.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
