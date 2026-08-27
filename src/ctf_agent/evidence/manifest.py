"""Hash-backed evidence manifest for reproducible CTF runs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class EvidenceEntry:
    label: str
    path: str
    sha256: str
    media_type: str
    created_at: str
    source: str | None = None
    redacted: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_file(
        cls,
        path: Path,
        *,
        root: Path,
        label: str,
        media_type: str,
        source: str | None = None,
        redacted: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> EvidenceEntry:
        return cls(
            label=label,
            path=path.relative_to(root).as_posix(),
            sha256=sha256_file(path),
            media_type=media_type,
            created_at=_now_iso(),
            source=source,
            redacted=redacted,
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "path": self.path,
            "sha256": self.sha256,
            "media_type": self.media_type,
            "created_at": self.created_at,
            "source": self.source,
            "redacted": self.redacted,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceEntry:
        return cls(
            label=str(data["label"]),
            path=str(data["path"]),
            sha256=str(data["sha256"]),
            media_type=str(data["media_type"]),
            created_at=str(data["created_at"]),
            source=data.get("source"),
            redacted=bool(data.get("redacted", False)),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class EvidenceEvent:
    stage: str
    message: str
    created_at: str = field(default_factory=_now_iso)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "message": self.message,
            "created_at": self.created_at,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceEvent:
        return cls(
            stage=str(data["stage"]),
            message=str(data["message"]),
            created_at=str(data["created_at"]),
            data=dict(data.get("data", {})),
        )


@dataclass
class EvidenceManifest:
    run_id: str
    entries: list[EvidenceEntry] = field(default_factory=list)
    events: list[EvidenceEvent] = field(default_factory=list)
    version: int = 1

    def add_file(
        self,
        path: Path,
        *,
        root: Path,
        label: str,
        media_type: str,
        source: str | None = None,
        redacted: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> EvidenceEntry:
        entry = EvidenceEntry.from_file(
            path,
            root=root,
            label=label,
            media_type=media_type,
            source=source,
            redacted=redacted,
            metadata=metadata,
        )
        self.entries.append(entry)
        return entry

    def add_event(self, stage: str, message: str, **data: Any) -> EvidenceEvent:
        event = EvidenceEvent(stage=stage, message=message, data=data)
        self.events.append(event)
        return event

    def verify_files(self, root: Path) -> list[str]:
        problems: list[str] = []
        for entry in self.entries:
            path = root / entry.path
            if not path.exists():
                problems.append(f"missing evidence file: {entry.path}")
                continue
            actual = sha256_file(path)
            if actual != entry.sha256:
                problems.append(
                    f"sha256 mismatch for {entry.path}: expected {entry.sha256}, got {actual}"
                )
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "entries": [entry.to_dict() for entry in self.entries],
            "events": [event.to_dict() for event in self.events],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceManifest:
        return cls(
            version=int(data.get("version", 1)),
            run_id=str(data["run_id"]),
            entries=[EvidenceEntry.from_dict(item) for item in data.get("entries", [])],
            events=[EvidenceEvent.from_dict(item) for item in data.get("events", [])],
        )

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"
        path.write_text(content, encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> EvidenceManifest:
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
