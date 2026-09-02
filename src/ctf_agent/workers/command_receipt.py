"""Controller receipt issuance for canonical solver commands."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ctf_agent.execution_receipts import ExecutionEvidence
from ctf_agent.reproduction import ReproductionSpec, controller_reproduction_spec
from ctf_agent.solver_command import canonical_network_host

if TYPE_CHECKING:
    from ctf_agent.workers.core import WorkerCore


@dataclass(frozen=True, slots=True)
class CommandExecutionEvidence:
    """Successful command values needed to issue a solver receipt."""

    argv: Sequence[str]
    stdout_path: Path
    stderr_path: Path
    command_fingerprint: str
    output_fingerprint: str


def issue_solver_receipt(
    worker: WorkerCore,
    evidence: CommandExecutionEvidence,
) -> tuple[str | None, ReproductionSpec | None]:
    """Issue authority only for canonical, scope-compatible lane solver execution."""
    if worker.execution_receipts is None or worker.lane_id is None:
        return None, None
    try:
        spec = controller_reproduction_spec(
            worker.workspace.root,
            worker.workspace.root,
            tuple(evidence.argv),
            requires_auth_handle=bool(
                worker.http_session is not None and worker.http_session.authenticated
            ),
        )
        network_host = canonical_network_host(spec.argv)
    except (FileNotFoundError, ValueError) as exc:
        worker._emit(
            "worker.command.receipt_rejected",
            {"fingerprint": evidence.command_fingerprint, "reason": str(exc)},
        )
        return None, None
    has_network_arguments = "--host" in spec.argv or "--port" in spec.argv
    network_arguments_scoped = not has_network_arguments or (
        worker.http_session is not None
        and (
            network_host is None
            or worker.http_session.scope.allows(f"https://{network_host}")
        )
    )
    if not network_arguments_scoped:
        worker._emit(
            "worker.command.receipt_rejected",
            {
                "fingerprint": evidence.command_fingerprint,
                "reason": "solver network arguments lack matching controller scope",
            },
        )
        return None, None
    receipt = worker.execution_receipts.issue(
        ExecutionEvidence(
            lane_id=worker.lane_id,
            argv=spec.argv,
            solver_path=spec.solver_path,
            stdout_path=evidence.stdout_path,
            stderr_path=evidence.stderr_path,
            command_fingerprint=evidence.command_fingerprint,
            output_fingerprint=evidence.output_fingerprint,
        )
    )
    return receipt, spec
