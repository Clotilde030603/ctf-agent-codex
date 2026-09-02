"""Stable aggregation for paired benchmark ablations."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

from ctf_agent.ablation_schema import AblationMatrix, PairedRunIdentity
from ctf_agent.ablation_statistics import distribution as _distribution
from ctf_agent.ablation_statistics import event_metrics as _event_metrics
from ctf_agent.ablation_statistics import failure_reason as _failure_reason
from ctf_agent.ablation_statistics import failure_reasons as _failure_reasons
from ctf_agent.ablation_statistics import wilson_interval as _wilson_interval
from ctf_agent.benchmark import (
    BenchmarkChallenge,
    BenchmarkManifest,
    BenchmarkRunRecord,
    solve_at_k,
)

RunPair = tuple[PairedRunIdentity, BenchmarkRunRecord]


def _solve_summary(runs: list[RunPair], condition_id: str, k: int) -> dict[str, Any]:
    attempts: dict[str, list[bool]] = defaultdict(list)
    for identity, record in runs:
        if identity.condition_id == condition_id:
            attempts[identity.case_id].append(record.solved)
    numerator = sum(solve_at_k(values, k) for values in attempts.values())
    denominator = len(attempts)
    rate = numerator / denominator if denominator else 0.0
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": rate,
        "confidence_interval": _wilson_interval(numerator, denominator),
    }


def _costs(records: list[BenchmarkRunRecord]) -> dict[str, float | str]:
    model = sum(record.metrics.model_cost for record in records)
    tool = sum(record.metrics.tool_cost for record in records)
    network = sum(record.metrics.network_cost for record in records)
    return {
        "model": model,
        "tool": tool,
        "network": network,
        "total": model + tool + network,
        "unit": "scorer_event_units",
    }


def build_ablation_report(
    manifest: BenchmarkManifest,
    matrix: AblationMatrix,
    runs: list[RunPair],
    solve_k: int,
) -> dict[str, Any]:
    """Aggregate only scorer-owned run fields into deterministic output."""
    by_condition: dict[str, list[BenchmarkRunRecord]] = defaultdict(list)
    for identity, record in runs:
        by_condition[identity.condition_id].append(record)
    condition_summaries: list[dict[str, Any]] = []
    for condition in matrix.conditions:
        records = by_condition[condition.condition_id]
        condition_summaries.append(
            {
                **condition.model_dump(mode="json"),
                "solve_at_1": _solve_summary(runs, condition.condition_id, 1),
                "solve_at_3": _solve_summary(runs, condition.condition_id, 3),
                "solve_at_k": _solve_summary(runs, condition.condition_id, solve_k),
                "costs": _costs(records),
                "latency_seconds": _distribution(
                    [
                        item.metrics.time_to_verified_seconds
                        for item in records
                        if item.metrics.time_to_verified_seconds is not None
                    ]
                ),
                "failure_reasons": dict(sorted(_failure_reasons(records).items())),
                "event_metrics": _event_metrics(records),
            }
        )
    baseline = condition_summaries[0]
    paired_deltas = [
        {
            "baseline": "B0",
            "condition_id": item["condition_id"],
            "solve_at_1_delta": item["solve_at_1"]["rate"]
            - baseline["solve_at_1"]["rate"],
            "solve_at_3_delta": item["solve_at_3"]["rate"]
            - baseline["solve_at_3"]["rate"],
        }
        for item in condition_summaries[1:]
    ]
    latencies = sorted(
        record.metrics.time_to_verified_seconds
        for _, record in runs
        if record.metrics.time_to_verified_seconds is not None
    )
    contamination = Counter(
        challenge.contamination.status
        for challenge in manifest.challenges
        if challenge.contamination
    )
    return {
        "schema_version": 1,
        "evaluation_id": matrix.evaluation_id,
        "dataset_revision": matrix.dataset_revision,
        "ablation_revision": matrix.ablation_revision,
        "challenge_identities": [_case_identity(item) for item in manifest.challenges],
        "condition_summaries": condition_summaries,
        "category_summaries": _category_summaries(manifest, runs),
        "paired_deltas": paired_deltas,
        "solve_at": {
            "1": baseline["solve_at_1"],
            "3": baseline["solve_at_3"],
            str(solve_k): baseline["solve_at_k"],
        },
        "costs": _costs([record for _, record in runs]),
        "latency_seconds": _distribution(latencies),
        "failure_reasons": dict(
            sorted(_failure_reasons([record for _, record in runs]).items())
        ),
        "operational_metrics": _event_metrics([record for _, record in runs]),
        "empirical_provenance_identities": [
            _empirical_identity(challenge)
            for challenge in manifest.challenges
            if not isinstance(challenge.difficulty, str)
            and challenge.difficulty.source == "empirical"
        ],
        "contamination": {
            "counts": dict(sorted(contamination.items())),
            "rates": {
                key: value / len(manifest.challenges)
                for key, value in sorted(contamination.items())
            },
            "interpretation": "disclosed separately; solve metrics are not adjusted",
        },
        "pairing": {"complete": True, "pair_count": len(runs)},
        "runs": [
            {
                "identity": identity.model_dump(mode="json"),
                "solved": record.solved,
                "latency_seconds": record.metrics.time_to_verified_seconds,
                "costs": _costs([record]),
                "authoritative_metrics_source": record.authoritative_metrics_source,
                "observed_runtime_identity": (
                    record.observed_runtime_identity.model_dump(mode="json")
                    if record.observed_runtime_identity is not None
                    else None
                ),
                "event_metrics": _event_metrics([record]),
                "failure_reason": _failure_reason(record),
            }
            for identity, record in runs
        ],
    }


def _case_identity(challenge: BenchmarkChallenge) -> dict[str, Any]:
    difficulty = challenge.difficulty
    redistribution = challenge.redistribution
    contamination = challenge.contamination
    return {
        "case_id": challenge.case_id,
        "category": challenge.category,
        "difficulty": difficulty.model_dump(mode="json")
        if not isinstance(difficulty, str)
        else difficulty,
        "fixture_sha256": challenge.fixture_sha256,
        "solution_sha256": challenge.solution_sha256,
        "authorized_for_benchmark": challenge.authorized_for_benchmark,
        "redistribution": redistribution.model_dump(mode="json") if redistribution else None,
        "contamination": contamination.model_dump(mode="json") if contamination else None,
    }


def _category_summaries(
    manifest: BenchmarkManifest,
    runs: list[RunPair],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for condition_id in ("B0", "B1", "B2", "B3", "B4", "B5"):
        for category in sorted({challenge.category for challenge in manifest.challenges}):
            case_ids = {
                challenge.case_id
                for challenge in manifest.challenges
                if challenge.category == category
            }
            attempts: dict[str, list[bool]] = defaultdict(list)
            for identity, record in runs:
                if identity.condition_id == condition_id and identity.case_id in case_ids:
                    attempts[identity.case_id].append(record.solved)
            result.append(
                {
                    "condition_id": condition_id,
                    "category": category,
                    "case_count": len(attempts),
                    "solve_at_1": sum(
                        solve_at_k(values, 1) for values in attempts.values()
                    )
                    / len(attempts),
                    "solve_at_3": sum(
                        solve_at_k(values, 3) for values in attempts.values()
                    )
                    / len(attempts),
                }
            )
    return result


def _empirical_identity(challenge: BenchmarkChallenge) -> dict[str, Any]:
    difficulty = challenge.difficulty
    if isinstance(difficulty, str):
        raise TypeError("empirical identity requires structured difficulty")
    return {
        "case_id": challenge.case_id,
        "reference_run": difficulty.source_value,
        "fixture_sha256": challenge.fixture_sha256,
        "solution_sha256": challenge.solution_sha256,
    }


def canonical_report_json(report: dict[str, Any]) -> str:
    """Serialize without volatile fields for byte-for-byte reproducibility."""
    return json.dumps(report, indent=2, sort_keys=True) + "\n"
