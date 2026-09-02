"""Typed durable state for resumable specialist lanes."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, NewType

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ctf_agent.schemas import SpecialistResult

LaneId = NewType("LaneId", str)
LANE_CHECKPOINT_SCHEMA_VERSION = 1


class LaneStatus(StrEnum):
    SOLVED = "solved"
    PROGRESS = "progress"
    STALLED = "stalled"
    FAILED = "failed"


class LaneModelIdentity(BaseModel):
    """Credential-free inputs that determine checkpoint compatibility."""

    model_config = ConfigDict(frozen=True)
    specialist: str
    model: str
    effort: str
    skill_sha256: str
    capability_sha256: str
    attachment_sha256: str


class CandidateHistoryEntry(BaseModel):
    """Secret-free history for one observed candidate."""

    model_config = ConfigDict(frozen=True)
    value_sha256: str
    source_artifact: str
    source_location: str
    confidence: float = Field(ge=0, le=1)
    observed_step: int = Field(ge=1)


class ProvenancedFact(BaseModel):
    """A fact with explicit trust status and controller provenance."""

    model_config = ConfigDict(frozen=True)
    fact: str
    source: Literal["model", "command", "artifact"]
    artifact: str | None = None
    command: tuple[str, ...] = ()
    evidence_sha256: str | None = None
    status: Literal["untrusted", "validated"]
    sequence: int = Field(ge=1)


class LaneCheckpoint(BaseModel):
    """Complete durable continuation state for one isolated specialist lane."""

    model_config = ConfigDict(frozen=True)
    lane_id: str
    run_id: str
    hypothesis_id: str
    hypothesis_revision: str
    category: str
    model_identity: LaneModelIdentity
    status: LaneStatus = LaneStatus.PROGRESS
    revision: int = Field(default=0, ge=0)
    step_index: int = Field(default=0, ge=0)
    hypothesis: str
    restatement: str
    verified_facts: tuple[str, ...] = ()
    facts: tuple[ProvenancedFact, ...] = ()
    failed_approaches: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    candidate_history: tuple[CandidateHistoryEntry, ...] = ()
    next_action: str = "begin hypothesis investigation"
    workspace_generation: int = Field(default=1, ge=1)
    command_fingerprints: tuple[str, ...] = ()
    output_fingerprints: tuple[str, ...] = ()
    written_file_hashes: dict[str, str] = Field(default_factory=dict)
    no_progress_steps: int = Field(default=0, ge=0)
    commands_run: int = Field(default=0, ge=0)
    http_requests_run: int = Field(default=0, ge=0)
    pending_step: int | None = Field(default=None, ge=1)
    pending_request_id: str | None = None
    pending_decision_json: str | None = None
    pending_decision_path: str | None = None
    completed_report_json: str | None = None
    completed_report_path: str | None = None
    schema_version: Literal[1] = 1
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def migrate_legacy_facts(self) -> LaneCheckpoint:
        if self.facts or not self.verified_facts:
            return self
        return self.model_copy(update={
            "facts": tuple(
                ProvenancedFact(
                    fact=fact,
                    source="model",
                    status="untrusted",
                    sequence=index,
                )
                for index, fact in enumerate(self.verified_facts, 1)
            )
        })

    @property
    def compatibility_fingerprint(self) -> str:
        return content_identity({"model_identity": self.model_identity.model_dump(mode="json")})


class LaneRunResult(BaseModel):
    """Specialist slice outcome plus its committed checkpoint."""

    model_config = ConfigDict(frozen=True)
    status: LaneStatus
    checkpoint: LaneCheckpoint
    specialist_result: SpecialistResult


def stable_lane_id(run_id: str, hypothesis_id: str, specialist: str) -> LaneId:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", hypothesis_id).strip("-") or "lane"
    digest = content_identity([run_id, hypothesis_id, specialist])[:12]
    return LaneId(f"{safe[:40]}-{digest}")


def content_identity(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
