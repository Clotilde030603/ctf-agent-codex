"""Lane workspace confinement and worker artifact aggregation."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from ctf_agent.schemas import FlagCandidate
from ctf_agent.security import protect_directory, protect_file
from ctf_agent.workers.models import WorkerExecutionError, WorkerReport


class LaneWorkspace:
    def __init__(
        self, root: Path | str, *, challenge_files: Path | str | None = None
    ) -> None:
        self.root = Path(root).resolve()
        protect_directory(self.root)
        self.artifacts_dir = self.root / "artifacts"
        protect_directory(self.artifacts_dir)
        self.challenge_files = (
            Path(challenge_files).resolve() if challenge_files is not None else None
        )

    def resolve_relative(self, relative_path: str) -> Path:
        value = Path(relative_path)
        if value.is_absolute():
            raise WorkerExecutionError(f"path must be relative: {relative_path}")
        if any(part in {"", ".", ".."} for part in value.parts):
            raise WorkerExecutionError(f"path contains unsafe segment: {relative_path}")
        target = (self.root / value).resolve()
        if target != self.root and self.root not in target.parents:
            raise WorkerExecutionError(f"path escapes lane workspace: {relative_path}")
        return target

    def write_relative_file(self, relative_path: str, content: str) -> Path:
        target = self.resolve_relative(relative_path)
        protect_directory(target.parent)
        target.write_text(content, encoding="utf-8")
        protect_file(target)
        return target


def truncate(value: bytes, limit: int) -> bytes:
    if len(value) <= limit:
        return value
    suffix = f"\n[truncated to {limit} bytes]\n".encode()
    return value[: max(0, limit - len(suffix))] + suffix


def findings_to_dict(findings: Iterable[Any]) -> dict[str, int]:
    output: dict[str, int] = {}
    for finding in findings:
        kind = str(finding.kind)
        output[kind] = output.get(kind, 0) + int(finding.count)
    return output


def merge_findings(*finding_groups: Iterable[Any]) -> dict[str, int]:
    output: dict[str, int] = {}
    for findings in finding_groups:
        for kind, count in findings_to_dict(findings).items():
            output[kind] = output.get(kind, 0) + count
    return output


def aggregate_reports(reports: Sequence[WorkerReport]) -> dict[str, Any]:
    facts: list[str] = []
    fact_seen: set[str] = set()
    candidates: list[FlagCandidate] = []
    candidate_seen: set[str] = set()
    written_files: list[str] = []
    written_seen: set[str] = set()
    for report in reports:
        for fact in report.facts:
            if fact not in fact_seen:
                fact_seen.add(fact)
                facts.append(fact)
        for candidate in report.flag_candidates:
            if candidate.value not in candidate_seen:
                candidate_seen.add(candidate.value)
                candidates.append(candidate)
        if report.written_path and report.written_path not in written_seen:
            written_seen.add(report.written_path)
            written_files.append(report.written_path)
    return {
        "facts": facts,
        "flag_candidates": candidates,
        "written_files": written_files,
    }
