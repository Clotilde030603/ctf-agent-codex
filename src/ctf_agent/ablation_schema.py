"""Frozen identities for cumulative paired benchmark ablations."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ctf_agent.config import ReasoningEffort

ConditionId = Literal["B0", "B1", "B2", "B3", "B4", "B5"]
CapabilityMode = Literal["current", "corrected"]
BudgetMode = Literal["shared", "elastic"]
FrontierMode = Literal["fixed", "adaptive"]
EXPECTED_CONDITIONS: tuple[ConditionId, ...] = ("B0", "B1", "B2", "B3", "B4", "B5")
_EXPECTED_FEATURES: dict[
    ConditionId, tuple[CapabilityMode, BudgetMode, bool, bool, FrontierMode]
] = {
    "B0": ("current", "shared", False, False, "fixed"),
    "B1": ("corrected", "shared", False, False, "fixed"),
    "B2": ("corrected", "elastic", False, False, "fixed"),
    "B3": ("corrected", "elastic", True, False, "fixed"),
    "B4": ("corrected", "elastic", True, True, "fixed"),
    "B5": ("corrected", "elastic", True, True, "adaptive"),
}


class InvalidEvaluationMetadata(ValueError):
    """An evaluation identity or frozen artifact is inconsistent."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class AblationCondition(BaseModel):
    """Explicit cumulative workflow feature and runtime identity."""

    model_config = ConfigDict(frozen=True)

    condition_id: ConditionId
    description: str
    capability_mode: CapabilityMode
    budget_mode: BudgetMode
    lane_continuity: bool
    context_projection: bool
    frontier_mode: FrontierMode
    model_id: str
    reasoning_id: ReasoningEffort
    tool_image_digest: str
    capability_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_ids: tuple[str, ...]
    solver_id: str
    artifact_id: str
    seed: int = Field(ge=0)
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def config_hash_matches_identity(self) -> AblationCondition:
        payload = self.model_dump(exclude={"config_sha256"}, mode="json")
        actual = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if actual != self.config_sha256:
            raise InvalidEvaluationMetadata(
                f"{self.condition_id} config_sha256 mismatch: "
                f"expected {self.config_sha256}, got {actual}"
            )
        expected = _EXPECTED_FEATURES[self.condition_id]
        observed = (
            self.capability_mode,
            self.budget_mode,
            self.lane_continuity,
            self.context_projection,
            self.frontier_mode,
        )
        if observed != expected:
            raise InvalidEvaluationMetadata(
                f"{self.condition_id} does not match the cumulative #3-#7 specification"
            )
        return self


class AblationMatrix(BaseModel):
    """A complete cumulative B0-B5 matrix bound to one evaluation."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[2]
    evaluation_id: str
    dataset_revision: str
    ablation_revision: str
    conditions: tuple[AblationCondition, ...]

    @model_validator(mode="after")
    def matrix_is_complete(self) -> AblationMatrix:
        identities = tuple(item.condition_id for item in self.conditions)
        if identities != EXPECTED_CONDITIONS:
            raise InvalidEvaluationMetadata("ablation matrix must contain exactly B0-B5 in order")
        return self


class ObservedRuntimeIdentity(BaseModel):
    """Scorer observation of configuration applied before process launch."""

    model_config = ConfigDict(frozen=True)

    capability_mode: CapabilityMode
    budget_mode: BudgetMode
    lane_continuity: bool
    context_projection: bool
    frontier_mode: FrontierMode
    model_id: str
    reasoning_id: ReasoningEffort
    tool_image_digest: str
    capability_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_ids: tuple[str, ...]
    solver_id: str
    artifact_id: str
    seed: int
    config_sha256: str


class PairedRunIdentity(BaseModel):
    """Immutable join key plus all scorer-relevant frozen identities."""

    model_config = ConfigDict(frozen=True)

    evaluation_id: str
    dataset_revision: str
    ablation_revision: str
    case_id: str
    condition_id: ConditionId
    repeat_index: int = Field(ge=1)
    model_id: str
    reasoning_id: ReasoningEffort
    tool_image_digest: str
    capability_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_ids: tuple[str, ...]
    solver_id: str
    artifact_id: str
    fixture_sha256: str
    solution_sha256: str
    config_sha256: str
    seed: int
