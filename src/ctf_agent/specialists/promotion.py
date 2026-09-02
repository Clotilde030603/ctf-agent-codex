"""Candidate reproduction evidence matching and promotion."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path

from ctf_agent.execution_receipts import ExecutionEvidence, ExecutionReceiptStore
from ctf_agent.schemas import FlagCandidate
from ctf_agent.workers import WorkerResult


@dataclass(frozen=True, slots=True)
class PromotionAuthority:
    """Controller authority required to promote lane execution evidence."""

    receipts: ExecutionReceiptStore
    lane_id: str
    solve_path: Path


def successful_argv(
    result: WorkerResult,
    candidate: FlagCandidate,
    authority: PromotionAuthority,
) -> tuple[str, ...] | None:
    declared = _declared_argv(candidate)
    if declared is None:
        return None
    for report in result.reports:
        report_argv = tuple(report.argv)
        if (
            report.action != "run"
            or report.status != "ok"
            or report.exit_code != 0
            or not report.stdout_artifact
            or not report.stderr_artifact
            or not report.command_fingerprint
            or not report.output_fingerprint
            or not report.execution_receipt
            or declared != report_argv
        ):
            continue
        stdout = Path(report.stdout_artifact)
        evidence = ExecutionEvidence(
            lane_id=authority.lane_id,
            argv=report_argv,
            solver_path=authority.solve_path,
            stdout_path=stdout,
            stderr_path=Path(report.stderr_artifact),
            command_fingerprint=report.command_fingerprint,
            output_fingerprint=report.output_fingerprint,
        )
        if authority.receipts.verifies(report.execution_receipt, evidence) and (
            candidate.value in stdout.read_text(encoding="utf-8", errors="replace")
        ):
            return report_argv
    return None


def successful_argv_from_artifacts(
    candidate: FlagCandidate,
    artifacts: tuple[str, ...],
    authority: PromotionAuthority,
) -> tuple[str, ...] | None:
    declared = _declared_argv(candidate)
    if declared is None:
        return None
    for artifact in artifacts:
        stdout = Path(artifact)
        if not stdout.name.endswith(".stdout.txt") or not stdout.is_file():
            continue
        prefix = stdout.name.removesuffix(".stdout.txt")
        metadata = stdout.with_name(f"{prefix}.meta.json")
        stderr = stdout.with_name(f"{prefix}.stderr.txt")
        if not metadata.is_file() or not stderr.is_file():
            continue
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        raw_argv = payload.get("argv")
        report_argv = tuple(str(item) for item in raw_argv) if isinstance(raw_argv, list) else ()
        receipt = payload.get("execution_receipt")
        command_fingerprint = payload.get("fingerprint")
        output_fingerprint = payload.get("output_fingerprint")
        if (
            payload.get("exit_code") != 0
            or declared != report_argv
            or not isinstance(receipt, str)
            or not isinstance(command_fingerprint, str)
            or not isinstance(output_fingerprint, str)
        ):
            continue
        evidence = ExecutionEvidence(
            lane_id=authority.lane_id,
            argv=report_argv,
            solver_path=authority.solve_path,
            stdout_path=stdout,
            stderr_path=stderr,
            command_fingerprint=command_fingerprint,
            output_fingerprint=output_fingerprint,
        )
        if authority.receipts.verifies(receipt, evidence) and candidate.value in stdout.read_text(
            encoding="utf-8", errors="replace"
        ):
            return report_argv
    return None


def _declared_argv(candidate: FlagCandidate) -> tuple[str, ...] | None:
    try:
        return tuple(shlex.split(candidate.solver_command))
    except ValueError:
        return None
