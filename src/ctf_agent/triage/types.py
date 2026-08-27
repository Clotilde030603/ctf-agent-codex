from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

ChallengeCategory = Literal[
    "web",
    "pwn",
    "rev",
    "crypto-math",
    "crypto-binary",
    "forensics",
    "misc",
    "mixed",
]


@dataclass(slots=True)
class ExtractedString:
    value: str
    offset: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Indicator:
    kind: str
    value: str
    artifact_path: str
    offset: int | None = None
    line: int | None = None
    context: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ToolRunResult:
    tool: str
    command: list[str]
    exit_code: int | None
    timed_out: bool
    duration_seconds: float
    stdout_artifact: str | None = None
    stderr_artifact: str | None = None
    missing: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ScannedFile:
    path: str
    relative_path: str
    size: int
    sha256: str
    mime: str
    magic: str
    entropy: float
    language: str | None
    parent_archive: str | None = None
    extraction_depth: int = 0
    strings: list[ExtractedString] = field(default_factory=list)
    indicators: list[Indicator] = field(default_factory=list)
    tool_results: list[ToolRunResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ExtractionRecord:
    archive_path: str
    extracted_path: str
    original_name: str
    depth: int
    size: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TriageReport:
    root: str
    files: list[ScannedFile] = field(default_factory=list)
    extractions: list[ExtractionRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    artifacts_dir: str | None = None

    def all_indicators(self) -> list[Indicator]:
        indicators: list[Indicator] = []
        for scanned in self.files:
            indicators.extend(scanned.indicators)
        return indicators

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ClassificationEvidence:
    category: ChallengeCategory
    reason: str
    artifact_path: str | None = None
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ClassificationResult:
    primary_category: ChallengeCategory
    secondary_categories: list[ChallengeCategory]
    confidence: float
    evidence: list[ClassificationEvidence]
    recommended_tools: list[str]
    missing_capabilities: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def path_to_text(path: Path) -> str:
    return path.as_posix()
