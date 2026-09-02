"""JSON provenance index generation for run evidence and write-ups."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ctf_agent.evidence.manifest import EvidenceManifest, sha256_file


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _file_record(path: Path, *, root: Path, media_type: str, label: str) -> dict[str, Any]:
    return {
        "label": label,
        "path": path.relative_to(root).as_posix(),
        "sha256": sha256_file(path),
        "media_type": media_type,
    }


def build_provenance_index(
    manifest: EvidenceManifest,
    *,
    root: Path,
    source_files: list[Path] | None = None,
    generated_outputs: list[tuple[Path, str, str]] | None = None,
    flag_reference: dict[str, str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a standalone JSON index without requiring a schema migration."""

    existing_sources = [path for path in source_files or [] if path.exists()]
    outputs = [
        _file_record(path, root=root, media_type=media_type, label=label)
        for path, media_type, label in generated_outputs or []
        if path.exists()
    ]
    return {
        "version": 1,
        "generated_at": _now_iso(),
        "run_id": manifest.run_id,
        "flag_reference": flag_reference or {},
        "source_files": [
            _file_record(path, root=root, media_type="application/json", label=path.name)
            for path in existing_sources
        ],
        "generated_outputs": outputs,
        "evidence_entries": [entry.to_dict() for entry in manifest.entries],
        "capture_failures": [failure.to_dict() for failure in manifest.failures],
        "events": [event.to_dict() for event in manifest.events],
        "metadata": dict(metadata or {}),
    }


def save_provenance_index(index: dict[str, Any], output: Path) -> Path:
    from ctf_agent.security import secure_write_json

    secure_write_json(output, index)
    return output
