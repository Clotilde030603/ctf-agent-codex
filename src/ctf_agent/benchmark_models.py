"""Typed benchmark command and run records."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ctf_agent.ablation_schema import ObservedRuntimeIdentity
from ctf_agent.benchmark_schema import BenchmarkRunIdentity, DifficultyProvenance


class BenchmarkMetrics(BaseModel):
    wrong_submissions: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    worker_command_calls: int = Field(default=0, ge=0)
    http_request_calls: int = Field(default=0, ge=0)
    hallucinated_candidates: int = Field(default=0, ge=0)
    candidate_count: int = Field(default=0, ge=0)
    rejected_candidates: int = Field(default=0, ge=0)
    time_to_candidate_seconds: float | None = Field(default=None, ge=0)
    time_to_verified_seconds: float | None = Field(default=None, ge=0)
    time_to_accepted_seconds: float | None = Field(default=None, ge=0)
    replay_verified: bool | None = None
    independent_verified: bool | None = None
    data_dependency_verified: bool | None = None
    evidence_completed: bool | None = None
    writeup_validated: bool | None = None
    resume_verified: bool | None = None
    total_run_status: str | None = None
    model_cost: float = Field(default=0, ge=0)
    tool_cost: float = Field(default=0, ge=0)
    network_cost: float = Field(default=0, ge=0)
    context_bytes: int = Field(default=0, ge=0)
    model_starvation_count: int = Field(default=0, ge=0)
    repeated_action_count: int = Field(default=0, ge=0)
    lane_retirement_count: int = Field(default=0, ge=0)
    lane_replacement_count: int = Field(default=0, ge=0)
    tcp_connect_count: int = Field(default=0, ge=0)
    restart_count: int = Field(default=0, ge=0)
    recovery_count: int = Field(default=0, ge=0)
    checkpoint_count: int = Field(default=0, ge=0)
    checkpoint_resume_count: int = Field(default=0, ge=0)
    budget_extension_count: int = Field(default=0, ge=0)
    projection_count: int = Field(default=0, ge=0)
    frontier_event_count: int = Field(default=0, ge=0)
    available_capability_count: int = Field(default=0, ge=0)
    elastic_budget_observed: int = Field(default=0, ge=0, le=1)
    lane_continuity_observed: int = Field(default=0, ge=0, le=1)
    adaptive_frontier_observed: int = Field(default=0, ge=0, le=1)


class CommandRecord(BaseModel):
    command: list[str]
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    seconds: float = 0
    timed_out: bool = False
    skipped: bool = False
    skip_reason: str | None = None


class BenchmarkRunRecord(BaseModel):
    challenge_id: str
    category: str
    difficulty: str | DifficultyProvenance
    repeat_index: int
    fixture_command_success: bool
    clean_replay_success: bool | None = None
    clean_replay_skipped: bool = False
    clean_replay_reason: str | None = None
    solved: bool
    expected_flag_seen: bool
    seconds_to_result: float
    timed_out: bool = False
    hardcoded_rejected: bool = False
    error: str | None = None
    command: CommandRecord | None = None
    clean_replay: CommandRecord | None = None
    metrics: BenchmarkMetrics = Field(default_factory=BenchmarkMetrics)
    self_reported_metrics: BenchmarkMetrics | None = None
    authoritative_metrics_source: Literal["scorer_command", "scorer_invocation"] = "scorer_command"
    observed_runtime_identity: ObservedRuntimeIdentity | None = None
    run_identity: BenchmarkRunIdentity | None = None
    verified_candidate: bool | None = None
    final_state: str | None = None
    promoted_solver_sha256: str | None = None
