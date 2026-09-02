"""Validated contracts for benchmark manifest v2 metadata and run identity."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from ctf_agent.skills import SkillIdentity


class InvalidBenchmarkMetadata(ValueError):
    """Manifest v2 metadata is missing or internally inconsistent."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class BenchmarkRunner(StrEnum):
    """Trusted execution path selected by the manifest."""

    FIXTURE_COMMAND = "fixture_command"
    AUTONOMOUS_WORKFLOW = "autonomous_workflow"


class DifficultyProvenance(BaseModel):
    """Difficulty label plus the evidence supporting it."""

    model_config = ConfigDict(frozen=True)

    label: Literal["easy", "medium", "hard"] | None = None
    source: Literal["published", "points", "solve_count", "empirical", "unknown"]
    source_value: str | int | float | None = None

    @model_validator(mode="after")
    def provenance_is_consistent(self) -> DifficultyProvenance:
        if self.source == "unknown" and (
            self.label is not None or self.source_value is not None
        ):
            raise InvalidBenchmarkMetadata(
                "unknown difficulty cannot claim a label or source value"
            )
        if self.source in {"points", "solve_count", "empirical"} and self.source_value is None:
            raise InvalidBenchmarkMetadata(
                f"{self.source} difficulty requires source_value"
            )
        if self.source == "published" and self.label is None:
            raise InvalidBenchmarkMetadata("published difficulty requires a label")
        return self


class RedistributionInfo(BaseModel):
    """Permission evidence for artifacts distributed with a benchmark."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    evidence_url: str | None = None

    @model_validator(mode="after")
    def allowed_redistribution_has_evidence(self) -> RedistributionInfo:
        if self.allowed and not self.evidence_url:
            raise InvalidBenchmarkMetadata(
                "allowed redistribution requires evidence_url"
            )
        return self


class ContaminationInfo(BaseModel):
    """Declared likelihood that a model has seen the fixture before evaluation."""

    model_config = ConfigDict(frozen=True)

    status: Literal["controlled", "likely_contaminated", "unknown"]
    details: str | None = None


class BenchmarkRunIdentity(BaseModel):
    """Identity joining one scorer record to one workflow run."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    challenge_id: str
    repeat_index: int
    runner: BenchmarkRunner
    tool_image_digest: str | None = None
    selected_skills: tuple[SkillIdentity, ...] = ()
