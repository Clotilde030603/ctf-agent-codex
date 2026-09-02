"""Typed contracts for deterministic model-context projection."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ProjectionAction(StrEnum):
    INCLUDED = "included"
    SUMMARIZED = "summarized"
    OMITTED = "omitted"


class TrustLabel(StrEnum):
    TRUSTED_POLICY = "trusted_policy"
    TRUSTED_RUNTIME = "trusted_runtime"
    UNTRUSTED_DATA = "untrusted_data"


class ProjectionSection(BaseModel):
    """One deterministic projection decision without secret-bearing content."""

    model_config = ConfigDict(frozen=True)

    order: int = Field(ge=0)
    section_id: str
    action: ProjectionAction
    mandatory: bool
    trust_label: TrustLabel
    provenance: str
    original_bytes: int = Field(ge=0)
    final_bytes: int = Field(ge=0)
    item_count: int = Field(ge=0)
    truncation_marker: str | None = None


class ProjectionManifest(BaseModel):
    """Machine-readable, deterministic account of one projection."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    policy_version: str
    role: str
    budget_bytes: int = Field(gt=0)
    section_quota_bytes: int = Field(gt=0)
    max_items_per_section: int = Field(gt=0)
    recent_report_limit: int = Field(ge=0)
    original_bytes: int = Field(ge=0)
    rendered_bytes: int = Field(ge=0)
    final_bytes: int = Field(ge=0)
    input_sha256: str
    original_sha256: str
    output_sha256: str
    final_sha256: str
    included: tuple[str, ...]
    summarized: tuple[str, ...]
    omitted: tuple[str, ...]
    sections: tuple[ProjectionSection, ...]


class ProjectedSection(BaseModel):
    """A rendered context section with an explicit trust boundary."""

    model_config = ConfigDict(frozen=True)

    section_id: str
    trust_label: TrustLabel
    provenance: str
    mandatory: bool
    content: Any
    truncation_marker: str | None = None


class ProjectedPrompt(BaseModel):
    """Final prompt bytes and their projection manifest."""

    model_config = ConfigDict(frozen=True)

    rendered: str
    manifest: ProjectionManifest
    sections: tuple[ProjectedSection, ...]
