"""Durable input loading and output paths for writeup generation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ctf_agent.evidence.manifest import EvidenceManifest


@dataclass(frozen=True, slots=True)
class WriteupContext:
    run_dir: Path
    challenge: dict[str, Any]
    triage: dict[str, Any]
    hypotheses: dict[str, Any] | list[Any]
    events: list[dict[str, Any]]
    evidence: EvidenceManifest
    solve_py: str
    generated_at: str


@dataclass(frozen=True, slots=True)
class WriteupOutputs:
    markdown_path: Path
    html_path: Path
    provenance_path: Path


def load_writeup_context(run_dir: Path) -> WriteupContext:
    manifest_path = run_dir / "evidence" / "manifest.json"
    evidence = (
        EvidenceManifest.load(manifest_path)
        if manifest_path.exists()
        else EvidenceManifest(run_id=run_dir.name)
    )
    return WriteupContext(
        run_dir=run_dir,
        challenge=_read_json(run_dir / "challenge.json", {}),
        triage=_read_json(run_dir / "triage.json", {}),
        hypotheses=_read_json(run_dir / "hypotheses.json", []),
        events=_read_jsonl(run_dir / "events.jsonl"),
        evidence=evidence,
        solve_py=_read_text(run_dir / "solve.py"),
        generated_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
    )


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")
