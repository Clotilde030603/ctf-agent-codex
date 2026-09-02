"""Typed benchmark manifest loading and validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, assert_never

from pydantic import BaseModel, Field, field_validator, model_validator

from ctf_agent import __version__
from ctf_agent.benchmark_schema import (
    BenchmarkRunner,
    ContaminationInfo,
    DifficultyProvenance,
    InvalidBenchmarkMetadata,
    RedistributionInfo,
)
from ctf_agent.config import DEFAULT_CTF_TOOL_IMAGE


class BenchmarkChallenge(BaseModel):
    id: str
    case_id: str | None = None
    fixture_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    solution_path: str | None = None
    solution_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    runner: BenchmarkRunner = BenchmarkRunner.FIXTURE_COMMAND
    command: list[str] = Field(default_factory=list)
    expected_flag: str | None = None
    expected_flag_sha256: str | None = None
    category: str = "misc"
    difficulty: str | DifficultyProvenance = "unknown"
    availability: Literal["active", "retired", "local"] | None = None
    source: str = "local"
    license: str = "unknown"
    retired: bool | None = None
    authorized_for_benchmark: bool | None = None
    redistribution: RedistributionInfo | None = None
    contamination: ContaminationInfo | None = None
    challenge_url: str | None = None
    artifact_paths: list[str] = Field(default_factory=list)
    flag_policy: dict[str, Any] = Field(default_factory=dict)
    expected_solver_capability: str = "deterministic"
    repeat: int | None = Field(default=None, ge=1)
    repeat_runs: int | None = Field(default=None, ge=1)
    timeout_seconds: float | None = Field(default=None, gt=0)
    total_budget_seconds: float | None = Field(default=None, gt=0)
    workdir: str = "."
    source_files: list[str] = Field(default_factory=list)
    metrics_file: str = "benchmark-metrics.json"
    events_file: str = "events.jsonl"
    metrics_source: Literal["none", "self_reported"] = "none"
    clean_replay: bool = True
    clean_mode: Literal["local", "docker"] = "local"
    replay_command: list[str] | None = None
    docker_image: str = DEFAULT_CTF_TOOL_IMAGE

    @field_validator("command", "replay_command")
    @classmethod
    def command_items_must_not_be_empty(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any(not item for item in value):
            raise ValueError("command entries must not be empty")
        return value

    @model_validator(mode="after")
    def expected_flag_source_required(self) -> BenchmarkChallenge:
        if self.expected_flag is None and self.expected_flag_sha256 is None:
            raise ValueError("expected_flag or expected_flag_sha256 is required")
        if self.authorized_for_benchmark is False:
            raise ValueError("benchmark challenge is not authorized for benchmark use")
        if self.repeat is not None and self.repeat_runs is None:
            self.repeat_runs = self.repeat
        return self

    @property
    def execution_group(self) -> str:
        return self.runner.value


class BenchmarkAgentIdentity(BaseModel):
    name: str = "ctf-agent-codex"
    version: str = __version__
    commit: str = "unknown"
    model: str | None = None
    reasoning_effort: str | None = None


class BenchmarkManifest(BaseModel):
    schema_version: Literal[1, 2] = 1
    evaluation_id: str | None = None
    dataset_revision: str | None = None
    ablation_revision: str | None = None
    agent: BenchmarkAgentIdentity = Field(default_factory=BenchmarkAgentIdentity)
    challenges: list[BenchmarkChallenge] = Field(default_factory=list)
    repeat_runs: int = Field(default=3, ge=1)
    timeout_seconds: float = Field(default=60, gt=0)
    total_budget_seconds: float = Field(default=3600, gt=0)

    @model_validator(mode="after")
    def v2_metadata_is_authoritative(self) -> BenchmarkManifest:
        challenge_ids = [challenge.id for challenge in self.challenges]
        if len(set(challenge_ids)) != len(challenge_ids):
            raise InvalidBenchmarkMetadata("duplicate benchmark challenge id")
        case_ids = [challenge.case_id for challenge in self.challenges if challenge.case_id]
        if len(set(case_ids)) != len(case_ids):
            raise InvalidBenchmarkMetadata("duplicate benchmark case_id")
        if self.schema_version == 1:
            return self
        for challenge in self.challenges:
            match challenge.difficulty:
                case DifficultyProvenance():
                    pass
                case str():
                    raise InvalidBenchmarkMetadata(
                        "manifest v2 requires structured difficulty provenance"
                    )
                case unreachable:
                    assert_never(unreachable)
            if challenge.authorized_for_benchmark is not True:
                raise InvalidBenchmarkMetadata(
                    "manifest v2 challenge must be authorized for benchmark use"
                )
            if challenge.redistribution is None or not challenge.redistribution.allowed:
                raise InvalidBenchmarkMetadata(
                    "manifest v2 requires allowed redistribution evidence"
                )
            if challenge.contamination is None:
                raise InvalidBenchmarkMetadata("manifest v2 requires contamination metadata")
            if challenge.availability is None:
                raise InvalidBenchmarkMetadata(
                    "manifest v2 requires availability separate from difficulty"
                )
            if (
                challenge.runner is BenchmarkRunner.AUTONOMOUS_WORKFLOW
                and not challenge.clean_replay
            ):
                raise InvalidBenchmarkMetadata(
                    "autonomous workflow benchmarks require clean replay"
                )
        return self


def _load_manifest(path: Path) -> BenchmarkManifest:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return BenchmarkManifest.model_validate(json.loads(text))
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("YAML manifests require the optional PyYAML package") from exc
    return BenchmarkManifest.model_validate(yaml.safe_load(text) or {})
