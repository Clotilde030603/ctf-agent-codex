"""Benchmark challenge aggregation and report serialization."""

from __future__ import annotations

import asyncio
import statistics
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ctf_agent.benchmark_execution import _run_once
from ctf_agent.benchmark_manifest import BenchmarkChallenge, BenchmarkManifest, _load_manifest
from ctf_agent.benchmark_models import BenchmarkRunRecord
from ctf_agent.benchmark_report_models import BenchmarkChallengeRecord, BenchmarkReport


async def run_benchmark(manifest: Path) -> dict[str, Any]:
    manifest = manifest.resolve()
    config = _load_manifest(manifest)
    started = time.monotonic()
    challenge_records: list[BenchmarkChallengeRecord] = []

    for challenge in config.challenges:
        if time.monotonic() - started >= config.total_budget_seconds:
            break
        challenge_records.append(await _run_challenge(manifest, config, challenge, started))

    runs = [run for challenge in challenge_records for run in challenge.runs]
    solved_count = sum(1 for item in challenge_records if item.solved)
    solved_run_count = sum(1 for item in runs if item.solved)
    fixture_successes = [run.fixture_command_success for run in runs]
    replay_successes = [
        run.clean_replay_success
        for run in runs
        if run.clean_replay_success is not None and not run.clean_replay_skipped
    ]

    report = BenchmarkReport(
        agent=config.agent,
        manifest=str(manifest),
        challenge_count=len(challenge_records),
        run_count=len(runs),
        total_elapsed_seconds=round(time.monotonic() - started, 6),
        solved_count=solved_count,
        solved_run_count=solved_run_count,
        solve_rate=_rate([item.solved for item in challenge_records]),
        solve_at_1=_rate(
            [solve_at_k([run.solved for run in item.runs], 1) for item in challenge_records]
        ),
        solve_at_3=_rate(
            [solve_at_k([run.solved for run in item.runs], 3) for item in challenge_records]
        ),
        fixture_command_success_rate=_rate(fixture_successes),
        clean_reproduction_rate=_rate(replay_successes),
        replay_verified_rate=_metric_rate(runs, "replay_verified"),
        independent_verified_rate=_metric_rate(runs, "independent_verified"),
        data_dependency_verified_rate=_metric_rate(runs, "data_dependency_verified"),
        evidence_completion_rate=_metric_rate(runs, "evidence_completed"),
        writeup_validated_rate=_metric_rate(runs, "writeup_validated"),
        resume_verified_rate=_metric_rate(runs, "resume_verified"),
        wrong_submissions=sum(item.wrong_submissions for item in challenge_records),
        model_calls=sum(item.model_calls for item in challenge_records),
        tool_calls=sum(item.tool_calls for item in challenge_records),
        worker_command_calls=sum(item.worker_command_calls for item in challenge_records),
        http_request_calls=sum(item.http_request_calls for item in challenge_records),
        hallucinated_candidate_rate=_candidate_hallucination_rate(runs),
        results=[_legacy_result(item) for item in challenge_records],
        challenges=challenge_records,
        group_summaries=_group_summaries(challenge_records),
        category_summaries=_category_summaries(challenge_records),
    )
    return report.model_dump(mode="json")


async def _run_challenge(
    manifest: Path,
    config: BenchmarkManifest,
    challenge: BenchmarkChallenge,
    benchmark_started: float,
) -> BenchmarkChallengeRecord:
    repeat_runs = challenge.repeat_runs or config.repeat_runs
    timeout_seconds = challenge.timeout_seconds or config.timeout_seconds
    challenge_budget = challenge.total_budget_seconds or config.total_budget_seconds
    runs: list[BenchmarkRunRecord] = []
    challenge_started = time.monotonic()
    for repeat_index in range(1, repeat_runs + 1):
        elapsed_total = time.monotonic() - benchmark_started
        elapsed_challenge = time.monotonic() - challenge_started
        remaining_total = config.total_budget_seconds - elapsed_total
        remaining_challenge = challenge_budget - elapsed_challenge
        if remaining_total <= 0 or remaining_challenge <= 0:
            break
        run_timeout = min(timeout_seconds, remaining_total, remaining_challenge)
        runs.append(
            await _run_once(
                manifest,
                challenge,
                repeat_index,
                timeout_seconds=run_timeout,
            )
        )

    replay_successes = [
        run.clean_replay_success
        for run in runs
        if run.clean_replay_success is not None and not run.clean_replay_skipped
    ]
    return BenchmarkChallengeRecord(
        id=challenge.id,
        category=challenge.category,
        difficulty=challenge.difficulty,
        availability=challenge.availability,
        redistribution=challenge.redistribution,
        contamination=challenge.contamination,
        execution_group=challenge.execution_group,
        repeat_runs=len(runs),
        solved=solve_at_k([run.solved for run in runs], len(runs)),
        fixture_command_success_rate=_rate([run.fixture_command_success for run in runs]),
        clean_replay_success_rate=_rate(replay_successes),
        wrong_submissions=sum(run.metrics.wrong_submissions for run in runs),
        model_calls=sum(run.metrics.model_calls for run in runs),
        tool_calls=sum(run.metrics.tool_calls for run in runs),
        worker_command_calls=sum(run.metrics.worker_command_calls for run in runs),
        http_request_calls=sum(run.metrics.http_request_calls for run in runs),
        hallucinated_candidate_rate=_candidate_hallucination_rate(runs),
        time_to_candidate_seconds=_median(run.metrics.time_to_candidate_seconds for run in runs),
        time_to_verified_seconds=_median(run.metrics.time_to_verified_seconds for run in runs),
        time_to_accepted_seconds=_median(run.metrics.time_to_accepted_seconds for run in runs),
        replay_verified_rate=_metric_rate(runs, "replay_verified"),
        independent_verified_rate=_metric_rate(runs, "independent_verified"),
        data_dependency_verified_rate=_metric_rate(runs, "data_dependency_verified"),
        evidence_completion_rate=_metric_rate(runs, "evidence_completed"),
        writeup_validated_rate=_metric_rate(runs, "writeup_validated"),
        resume_verified_rate=_metric_rate(runs, "resume_verified"),
        runs=runs,
    )


def _legacy_result(challenge: BenchmarkChallengeRecord) -> dict[str, Any]:
    first_run = challenge.runs[0] if challenge.runs else None
    return {
        "id": challenge.id,
        "category": challenge.category,
        "difficulty": challenge.difficulty,
        "solved": challenge.solved,
        "seconds_to_result": first_run.seconds_to_result if first_run else 0,
        "solved_at_15m": challenge.solved
        and first_run is not None
        and first_run.seconds_to_result <= 900,
        "solved_at_30m": challenge.solved
        and first_run is not None
        and first_run.seconds_to_result <= 1800,
        "solved_at_60m": challenge.solved
        and first_run is not None
        and first_run.seconds_to_result <= 3600,
        "wrong_submissions": challenge.wrong_submissions,
        "hallucinated_candidate_rate": challenge.hallucinated_candidate_rate,
        "clean_reproduction": challenge.clean_replay_success_rate == 1,
        "clean_reproduction_rate": challenge.clean_replay_success_rate,
        "fixture_command_success_rate": challenge.fixture_command_success_rate,
        "exit_code": first_run.command.exit_code if first_run and first_run.command else 127,
        "stderr": first_run.command.stderr if first_run and first_run.command else "",
        "error": first_run.error if first_run else "not run",
    }


def _category_summaries(
    challenges: list[BenchmarkChallengeRecord],
) -> list[dict[str, Any]]:
    categories: dict[str, list[BenchmarkChallengeRecord]] = {}
    for challenge in challenges:
        categories.setdefault(challenge.category, []).append(challenge)
    return [
        {
            "category": name,
            "challenge_count": len(items),
            "solved_count": sum(item.solved for item in items),
            "verified_solve_rate": _rate([item.solved for item in items]),
            "solve_at_1": _rate(
                [solve_at_k([run.solved for run in item.runs], 1) for item in items]
            ),
            "solve_at_3": _rate(
                [solve_at_k([run.solved for run in item.runs], 3) for item in items]
            ),
        }
        for name, items in sorted(categories.items())
    ]


def _group_summaries(
    challenges: list[BenchmarkChallengeRecord],
) -> list[dict[str, Any]]:
    groups: dict[str, list[BenchmarkChallengeRecord]] = {}
    for challenge in challenges:
        groups.setdefault(challenge.execution_group, []).append(challenge)
    return [
        {
            "group": name,
            "challenge_count": len(items),
            "solved_count": sum(item.solved for item in items),
            "solve_rate": _rate([item.solved for item in items]),
            "wrong_submissions": sum(item.wrong_submissions for item in items),
            "model_calls": sum(item.model_calls for item in items),
            "tool_calls": sum(item.tool_calls for item in items),
            "worker_command_calls": sum(item.worker_command_calls for item in items),
            "http_request_calls": sum(item.http_request_calls for item in items),
        }
        for name, items in sorted(groups.items())
    ]


def solve_at_k(attempts: Iterable[bool], k: int) -> bool:
    """Return whether any verified solve occurred among the first ``k`` attempts."""
    return any(value for index, value in enumerate(attempts) if index < k)


def _rate(values: Iterable[bool | None]) -> float | None:
    filtered = [value for value in values if value is not None]
    if not filtered:
        return None
    return sum(1 for value in filtered if value) / len(filtered)


def _metric_rate(runs: Iterable[BenchmarkRunRecord], field: str) -> float | None:
    return _rate(getattr(run.metrics, field) for run in runs)


def _candidate_hallucination_rate(runs: Iterable[BenchmarkRunRecord]) -> float | None:
    run_list = list(runs)
    candidate_count = sum(run.metrics.candidate_count for run in run_list)
    if candidate_count == 0:
        return None
    return sum(run.metrics.hallucinated_candidates for run in run_list) / candidate_count


def _median(values: Iterable[float | None]) -> float | None:
    filtered = [value for value in values if value is not None]
    if not filtered:
        return None
    return statistics.median(filtered)


def benchmark(manifest: Path) -> dict[str, Any]:
    return asyncio.run(run_benchmark(manifest))
