"""Flag candidate and provenance normalization helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ctf_agent.reproduction import ReproductionSpec


@dataclass(frozen=True, slots=True)
class Provenance:
    source: str
    method: str
    artifact: Path | None = None
    location: str | None = None
    command: tuple[str, ...] = ()
    observed_at: str | None = None

    @classmethod
    def from_schema(cls, value: Any) -> Provenance:
        data = _as_mapping(value)
        source = str(
            data.get("source") or data.get("kind") or data.get("source_artifact") or ""
        ).strip()
        derivation = data.get("derivation") or ""
        if isinstance(derivation, list):
            derivation = "; ".join(str(item) for item in derivation if item)
        method = str(
            data.get("method")
            or data.get("description")
            or derivation
            or data.get("source_location")
            or ""
        ).strip()
        artifact = (
            data.get("artifact")
            or data.get("artifact_path")
            or data.get("source_artifact")
            or data.get("path")
        )
        command = data.get("command") or data.get("solver_command") or ()
        observed_at = data.get("observed_at") or data.get("timestamp")
        location = data.get("location") or data.get("source_location")
        return cls(
            source=source,
            method=method,
            artifact=Path(str(artifact)) if artifact else None,
            location=str(location) if location else None,
            command=_coerce_command(command),
            observed_at=str(observed_at) if observed_at else None,
        )

    def is_actionable(self) -> bool:
        return bool(self.source and (self.method or self.artifact or self.location or self.command))


@dataclass(frozen=True, slots=True)
class FlagCandidate:
    value: str
    provenance: tuple[Provenance, ...] = field(default_factory=tuple)
    rejected: bool = False
    reject_reason: str | None = None
    solver: str | None = None
    format_match: bool = False
    replay_verified: bool = False
    independent_verified: bool = False
    reproduction_spec: ReproductionSpec | None = None

    @classmethod
    def from_schema(cls, value: Any) -> FlagCandidate:
        if isinstance(value, str):
            return cls(value=value)

        data = _as_mapping(value)
        raw_provenance = data.get("provenance") or data.get("evidence") or ()
        provenance_values: tuple[Any, ...]
        if isinstance(raw_provenance, Mapping):
            provenance_values = (raw_provenance,)
        else:
            provenance_values = tuple(raw_provenance or ())
        schema_provenance = _schema_candidate_provenance(data)
        if schema_provenance:
            provenance_values = (*provenance_values, schema_provenance)

        reject_reason = data.get("reject_reason") or data.get("rejection_reason")
        solver = data.get("solver") or data.get("solver_id") or data.get("solver_command")
        return cls(
            value=str(data.get("value") or data.get("flag") or data.get("candidate") or ""),
            provenance=tuple(Provenance.from_schema(item) for item in provenance_values),
            rejected=bool(data.get("rejected") or data.get("is_rejected") or False),
            reject_reason=str(reject_reason) if reject_reason else None,
            solver=str(solver) if solver else None,
            format_match=bool(data.get("format_match") or False),
            replay_verified=bool(data.get("replay_verified") or False),
            independent_verified=bool(data.get("independent_verified") or False),
            reproduction_spec=(
                ReproductionSpec.model_validate(data["reproduction_spec"])
                if data.get("reproduction_spec") is not None
                else None
            ),
        )

    @property
    def normalized_value(self) -> str:
        return normalize_flag(self.value)

    def has_actionable_provenance(self) -> bool:
        return any(item.is_actionable() for item in self.provenance)


def normalize_flag(value: str) -> str:
    return value.strip()


def _schema_candidate_provenance(data: Mapping[str, Any]) -> Mapping[str, Any] | None:
    keys = ("source_artifact", "source_location", "derivation", "solver_command")
    if not any(data.get(key) for key in keys):
        return None
    return {
        "source_artifact": data.get("source_artifact"),
        "source_location": data.get("source_location"),
        "derivation": data.get("derivation"),
        "solver_command": data.get("solver_command"),
    }


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, Mapping):
            return dumped
    if hasattr(value, "dict"):
        dumped = value.dict()
        if isinstance(dumped, Mapping):
            return dumped
    names = (
        "value",
        "flag",
        "candidate",
        "provenance",
        "evidence",
        "rejected",
        "is_rejected",
        "reject_reason",
        "rejection_reason",
        "solver",
        "solver_id",
        "source",
        "kind",
        "method",
        "description",
        "artifact",
        "artifact_path",
        "path",
        "location",
        "command",
        "observed_at",
        "timestamp",
        "source_artifact",
        "source_location",
        "derivation",
        "solver_command",
        "format_match",
        "replay_verified",
        "independent_verified",
        "reproduction_spec",
    )
    return {name: getattr(value, name) for name in names if hasattr(value, name)}


def _coerce_command(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(part for part in value.split() if part)
    if isinstance(value, list | tuple):
        return tuple(str(part) for part in value)
    return ()
