"""Deterministic statistics and event summaries for ablation reports."""

from __future__ import annotations

import math
import statistics
from collections import Counter
from typing import Any

from ctf_agent.benchmark_models import BenchmarkRunRecord


def wilson_interval(successes: int, total: int) -> dict[str, float]:
    if total == 0:
        return {"low": 0.0, "high": 0.0}
    z = 1.959963984540054
    rate = successes / total
    denominator = 1 + z * z / total
    center = (rate + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total))
        / denominator
    )
    return {"low": max(0.0, center - margin), "high": min(1.0, center + margin)}


def distribution(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {"median": None, "p95": None, "iqr": {"q1": None, "q3": None}}
    if len(ordered) == 1:
        q1 = q3 = ordered[0]
    else:
        q1, _, q3 = statistics.quantiles(ordered, n=4, method="inclusive")
    return {
        "median": statistics.median(ordered),
        "p95": ordered[math.ceil(0.95 * len(ordered)) - 1],
        "iqr": {"q1": q1, "q3": q3},
    }


def failure_reason(record: BenchmarkRunRecord) -> str | None:
    if record.error is not None:
        return record.error
    if not record.solved:
        return record.final_state or "unsolved"
    return None


def failure_reasons(records: list[BenchmarkRunRecord]) -> Counter[str]:
    return Counter(
        reason for record in records if (reason := failure_reason(record)) is not None
    )


def event_metrics(records: list[BenchmarkRunRecord]) -> dict[str, int]:
    fields = (
        "context_bytes",
        "model_starvation_count",
        "repeated_action_count",
        "lane_retirement_count",
        "lane_replacement_count",
        "tcp_connect_count",
        "restart_count",
        "recovery_count",
        "checkpoint_count",
        "checkpoint_resume_count",
        "budget_extension_count",
        "projection_count",
        "frontier_event_count",
        "available_capability_count",
        "elastic_budget_observed",
        "lane_continuity_observed",
        "adaptive_frontier_observed",
    )
    return {
        field: sum(int(getattr(record.metrics, field)) for record in records)
        for field in fields
    }
