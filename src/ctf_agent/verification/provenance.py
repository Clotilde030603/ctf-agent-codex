"""Safe provenance checks for flag candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .candidate import FlagCandidate, Provenance


@dataclass(frozen=True, slots=True)
class ProvenanceCheck:
    accepted: bool
    reason: str
    candidate: FlagCandidate
    artifact_path: Path | None = None
    provenance: Provenance | None = None


@dataclass(frozen=True, slots=True)
class ProvenanceVerifier:
    run_dir: Path

    def verify(self, candidate_value: object) -> ProvenanceCheck:
        candidate = FlagCandidate.from_schema(candidate_value)
        if not candidate.provenance:
            return ProvenanceCheck(False, "candidate has no provenance", candidate)

        rejected_reasons: list[str] = []
        for provenance in candidate.provenance:
            if provenance.artifact is None:
                rejected_reasons.append("provenance has no source artifact")
                continue
            artifact = self._resolve_artifact(provenance.artifact)
            if artifact is None:
                rejected_reasons.append(
                    f"source artifact is missing or outside run_dir: {provenance.artifact}"
                )
                continue
            bounds_reason = _validate_location_bounds(artifact, provenance.location)
            if bounds_reason is not None:
                rejected_reasons.append(bounds_reason)
                continue
            return ProvenanceCheck(
                True,
                "source artifact provenance is confined to run_dir",
                candidate,
                artifact,
                provenance,
            )

        reason = "; ".join(dict.fromkeys(rejected_reasons)) or "no usable provenance"
        return ProvenanceCheck(False, reason, candidate)

    def _resolve_artifact(self, artifact: Path) -> Path | None:
        root = self.run_dir.resolve()
        if artifact.is_absolute():
            resolved = artifact.resolve()
        else:
            if any(part == ".." for part in artifact.parts):
                return None
            resolved = (root / artifact).resolve()
        if resolved != root and root not in resolved.parents:
            return None
        if not resolved.is_file():
            return None
        return resolved


def _validate_location_bounds(artifact: Path, location: str | None) -> str | None:
    if not location:
        return None

    offset = _parse_offset(location)
    if offset is not None:
        size = artifact.stat().st_size
        if offset < 0 or offset > size:
            return f"source offset is outside artifact bounds: {offset}"
        return None

    line_number = _parse_line(location, artifact)
    if line_number is not None:
        line_count = _count_lines(artifact)
        if line_number < 1 or line_number > line_count:
            return f"source line is outside artifact bounds: {line_number}"
    return None


def _parse_offset(location: str) -> int | None:
    match = re.search(r"\boffset\s+(?P<offset>-?\d+)\b", location, re.IGNORECASE)
    return int(match.group("offset")) if match else None


def _parse_line(location: str, artifact: Path) -> int | None:
    line_match = re.search(r"\bline\s+(?P<line>-?\d+)\b", location, re.IGNORECASE)
    if line_match:
        return int(line_match.group("line"))

    path_line_match = re.search(r":(?P<line>-?\d+)(?::\d+)?$", location)
    if not path_line_match:
        return None
    prefix = location[: path_line_match.start()]
    if (
        prefix
        and Path(prefix).name != artifact.name
        and not artifact.as_posix().endswith(prefix)
    ):
        return None
    return int(path_line_match.group("line"))


def _count_lines(path: Path) -> int:
    data = path.read_bytes()
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)
