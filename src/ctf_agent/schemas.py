"""Validated data contracts shared by all workflow stages."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunState(StrEnum):
    AUTHENTICATE = "AUTHENTICATE"
    INGEST = "INGEST"
    TRIAGE = "TRIAGE"
    PLAN = "PLAN"
    SOLVE = "SOLVE"
    VERIFY = "VERIFY"
    SUBMIT = "SUBMIT"
    EVIDENCE = "EVIDENCE"
    WRITEUP = "WRITEUP"
    REPRODUCE = "REPRODUCE"
    DONE = "DONE"
    FAILED = "FAILED"


class AuthSession(BaseModel):
    authenticated: bool
    cookies: dict[str, str] = Field(default_factory=dict, repr=False)
    headers: dict[str, str] = Field(default_factory=dict, repr=False)
    expires_at: datetime | None = None
    storage_state: Path | None = Field(default=None, exclude=True)


class Artifact(BaseModel):
    path: Path
    sha256: str
    size: int = Field(ge=0)
    source_url: str | None = None
    parent_sha256: str | None = None
    media_type: str | None = None


class FlagPolicy(BaseModel):
    pattern: str = r"[A-Za-z0-9_]+\{[^\r\n{}]{1,256}\}"
    prefix: str | None = None
    case_sensitive: bool = True
    examples: list[str] = Field(default_factory=list)


class Challenge(BaseModel):
    id: str
    url: str
    event: str = "unknown-event"
    title: str
    description: str = ""
    category: str = "misc"
    points: int | None = None
    flag_policy: FlagPolicy = Field(default_factory=FlagPolicy)
    attachment_urls: list[str] = Field(default_factory=list)
    service_hosts: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Classification(BaseModel):
    primary_category: str
    secondary_categories: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    recommended_tools: list[str] = Field(default_factory=list)
    missing_capabilities: list[str] = Field(default_factory=list)


class Hypothesis(BaseModel):
    id: str
    claim: str
    supporting_evidence: list[str] = Field(default_factory=list)
    expected_signal: str
    cost: str = Field(pattern=r"^(low|medium|high)$")
    confidence: float = Field(ge=0, le=1)
    required_tools: list[str] = Field(default_factory=list)
    kill_condition: str
    success_condition: str


class FlagCandidate(BaseModel):
    value: str
    source_artifact: str
    source_location: str
    derivation: list[str] = Field(default_factory=list)
    solver_command: str
    format_match: bool = False
    replay_verified: bool = False
    independent_verified: bool = False
    confidence: float = Field(default=0, ge=0, le=1)

    @field_validator("value")
    @classmethod
    def reject_control_characters(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("flag candidates may not contain control characters")
        return value


class SpecialistResult(BaseModel):
    hypothesis_id: str
    status: str = Field(pattern=r"^(confirmed|rejected|inconclusive)$")
    facts: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    reproduction_command: str = ""
    flag_candidates: list[FlagCandidate] = Field(default_factory=list)
    next_action: str = ""
    confidence: float = Field(default=0, ge=0, le=1)


class SubmissionVerdict(StrEnum):
    ACCEPTED = "accepted"
    WRONG = "wrong"
    ALREADY_SOLVED = "already_solved"
    RATE_LIMITED = "rate_limited"
    UNKNOWN = "unknown"


class SubmissionResult(BaseModel):
    verdict: SubmissionVerdict
    message: str = ""
    status_code: int | None = None
    submitted_at: datetime = Field(default_factory=utc_now)


class VerificationResult(BaseModel):
    approved: bool
    reasons: list[str] = Field(default_factory=list)
    candidate: FlagCandidate


class EvidenceItem(BaseModel):
    path: Path
    sha256: str
    created_at: datetime = Field(default_factory=utc_now)
    evidence_type: str
    event_id: int | None = None
    sanitized: bool = False


class RunRecord(BaseModel):
    run_id: str
    challenge_url: str
    run_dir: Path
    state: RunState = RunState.AUTHENTICATE
    auto_submit: bool = False
    writeup: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_error: str | None = None
