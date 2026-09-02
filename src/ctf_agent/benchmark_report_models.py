"""Typed benchmark aggregate report records."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from ctf_agent.benchmark_manifest import BenchmarkAgentIdentity
from ctf_agent.benchmark_models import BenchmarkRunRecord
from ctf_agent.benchmark_schema import (
    ContaminationInfo,
    DifficultyProvenance,
    RedistributionInfo,
)


class BenchmarkChallengeRecord(BaseModel):
    id: str
    category: str
    difficulty: str | DifficultyProvenance
    availability: Literal["active", "retired", "local"] | None = None
    redistribution: RedistributionInfo | None = None
    contamination: ContaminationInfo | None = None
    execution_group: str
    repeat_runs: int
    solved: bool
    fixture_command_success_rate: float | None
    clean_replay_success_rate: float | None
    wrong_submissions: int
    model_calls: int
    tool_calls: int
    worker_command_calls: int
    http_request_calls: int
    hallucinated_candidate_rate: float | None
    time_to_candidate_seconds: float | None
    time_to_verified_seconds: float | None
    time_to_accepted_seconds: float | None
    replay_verified_rate: float | None
    independent_verified_rate: float | None
    data_dependency_verified_rate: float | None
    evidence_completion_rate: float | None
    writeup_validated_rate: float | None
    resume_verified_rate: float | None
    runs: list[BenchmarkRunRecord]


class BenchmarkReport(BaseModel):
    agent: BenchmarkAgentIdentity
    manifest: str
    challenge_count: int
    run_count: int
    total_elapsed_seconds: float
    solved_count: int
    solved_run_count: int
    solve_rate: float | None
    solve_at_1: float | None
    solve_at_3: float | None
    fixture_command_success_rate: float | None
    clean_reproduction_rate: float | None
    replay_verified_rate: float | None
    independent_verified_rate: float | None
    data_dependency_verified_rate: float | None
    evidence_completion_rate: float | None
    writeup_validated_rate: float | None
    resume_verified_rate: float | None
    wrong_submissions: int
    model_calls: int
    tool_calls: int
    worker_command_calls: int
    http_request_calls: int
    hallucinated_candidate_rate: float | None
    results: list[dict[str, Any]]
    challenges: list[BenchmarkChallengeRecord]
    group_summaries: list[dict[str, Any]]
    category_summaries: list[dict[str, Any]]
