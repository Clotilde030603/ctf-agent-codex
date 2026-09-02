"""Command policy, sandbox construction, and command execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from ctf_agent.capabilities import CapabilityStatus, RuntimeCapabilitySnapshot
from ctf_agent.capability_manifest import DEFAULT_CAPABILITY_MANIFEST
from ctf_agent.config import DEFAULT_CTF_TOOL_IMAGE
from ctf_agent.security import protect_file
from ctf_agent.workers.artifacts import merge_findings, truncate
from ctf_agent.workers.command_receipt import CommandExecutionEvidence, issue_solver_receipt
from ctf_agent.workers.models import WorkerDecision, WorkerExecutionError, WorkerReport

if TYPE_CHECKING:
    from ctf_agent.workers.core import WorkerCore


def _manifest_allowed_argv0() -> set[str]:
    return {
        item.command
        for item in DEFAULT_CAPABILITY_MANIFEST.capabilities
        if item.command is not None and item.allowed_by_default
    }


class CommandPolicy(BaseModel):
    allowed_argv0: set[str] | None = None
    docker_image: str = DEFAULT_CTF_TOOL_IMAGE
    docker_binary: str = "docker"
    cpus: str = "1"
    memory: str = "512m"
    pids_limit: int = Field(default=128, ge=1)
    local_test_mode: bool = False
    runtime_capabilities: RuntimeCapabilitySnapshot | None = None

    @property
    def effective_allowed_argv0(self) -> set[str]:
        if self.allowed_argv0 is not None:
            return self.allowed_argv0
        if self.runtime_capabilities is not None:
            return {
                item.name
                for item in self.runtime_capabilities.capabilities
                if item.category.value == "tool" and item.allowed
            }
        return _manifest_allowed_argv0()

    def validate_argv(self, argv: Sequence[str]) -> None:
        if not argv:
            raise WorkerExecutionError("argv must not be empty")
        argv0 = Path(argv[0]).name
        if self.runtime_capabilities is not None and not self.local_test_mode:
            capability = self.runtime_capabilities.require(argv0)
            if capability.status is not CapabilityStatus.AVAILABLE:
                raise WorkerExecutionError(
                    f"argv executable is {capability.status.value}: {argv0}; "
                    f"{capability.reason}"
                )
            if self.allowed_argv0 is None:
                return
        if argv0 not in self.effective_allowed_argv0:
            raise WorkerExecutionError(f"argv executable is not allowlisted: {argv0}")


def command_fingerprint(
    argv: Sequence[str],
    *,
    workspace_root: Path | None = None,
    workspace_generation: int = 0,
) -> str:
    file_hashes: list[tuple[int, str]] = []
    if workspace_root is not None:
        for index, argument in enumerate(argv):
            candidate = Path(argument)
            if not candidate.is_absolute():
                candidate = workspace_root / candidate
            if candidate.is_file() and workspace_root in candidate.resolve().parents:
                file_hashes.append((index, hashlib.sha256(candidate.read_bytes()).hexdigest()))
    payload = json.dumps(
        {
            "argv": list(argv),
            "file_hashes": file_hashes,
            "workspace_generation": workspace_generation,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def execution_command(worker: WorkerCore, argv: Sequence[str]) -> list[str]:
    if worker.policy.local_test_mode:
        return list(argv)

    command = [
        worker.policy.docker_binary,
        "run",
        "--rm",
        "--network=none",
        f"--cpus={worker.policy.cpus}",
        f"--memory={worker.policy.memory}",
        f"--pids-limit={worker.policy.pids_limit}",
        f"--user={_container_user()}",
        "--read-only",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
        f"--mount=type=bind,src={worker.workspace.root},dst=/work",
    ]
    if worker.workspace.challenge_files is not None:
        command.append(
            f"--mount=type=bind,src={worker.workspace.challenge_files},dst=/challenge,readonly"
        )
    command.extend(["-w", "/work", worker.policy.docker_image])
    command.extend(argv)
    return command


async def run_command(
    worker: WorkerCore, step: int, decision: WorkerDecision
) -> WorkerReport:
    argv = decision.argv
    worker.policy.validate_argv(argv)
    fingerprint = command_fingerprint(
        argv,
        workspace_root=worker.workspace.root,
        workspace_generation=worker._workspace_generation,
    )
    if fingerprint in worker._seen_commands:
        progress = worker._capture_decision_progress(decision)
        return WorkerReport(
            step=step,
            action="run",
            status="skipped",
            message="duplicate command fingerprint",
            argv=list(argv),
            command_fingerprint=fingerprint,
            facts=decision.facts,
            flag_candidates=decision.flag_candidates,
            made_progress=progress,
        )
    worker._seen_commands.add(fingerprint)

    command = execution_command(worker, argv)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=worker.workspace.root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise WorkerExecutionError(f"could not start command: {exc}") from exc
    timed_out = False
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), worker.budget.command_timeout_seconds
        )
    except TimeoutError:
        timed_out = True
        process.kill()
        stdout_bytes, stderr_bytes = await process.communicate()

    stdout = worker.sanitizer.sanitize(truncate(stdout_bytes, worker.budget.stdout_limit))
    stderr = worker.sanitizer.sanitize(truncate(stderr_bytes, worker.budget.stderr_limit))
    artifact_prefix = f"{step:03d}-{fingerprint[:16]}"
    stdout_path = worker.workspace.artifacts_dir / f"{artifact_prefix}.stdout.txt"
    stderr_path = worker.workspace.artifacts_dir / f"{artifact_prefix}.stderr.txt"
    metadata_path = worker.workspace.artifacts_dir / f"{artifact_prefix}.meta.json"
    stdout_path.write_text(stdout.text, encoding="utf-8")
    stderr_path.write_text(stderr.text, encoding="utf-8")
    protect_file(stdout_path)
    protect_file(stderr_path)
    exit_code = process.returncode
    if timed_out:
        exit_code = 124
    output_fingerprint = hashlib.sha256(
        stdout.text.encode("utf-8") + b"\0" + stderr.text.encode("utf-8")
    ).hexdigest()
    accepted = exit_code == 0 and not timed_out
    execution_receipt: str | None = None
    if accepted:
        execution_receipt, spec = issue_solver_receipt(
            worker,
            CommandExecutionEvidence(
                argv=argv,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                command_fingerprint=fingerprint,
                output_fingerprint=output_fingerprint,
            ),
        )
        if spec is not None:
            decision.flag_candidates = [
                candidate.model_copy(update={"reproduction_spec": spec})
                for candidate in decision.flag_candidates
            ]
    metadata = {
        "argv": list(argv),
        "command": command,
        "fingerprint": fingerprint,
        "output_fingerprint": output_fingerprint,
        "execution_receipt": execution_receipt,
        "exit_code": exit_code,
        "timed_out": timed_out,
    }
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    protect_file(metadata_path)
    novel_output = output_fingerprint not in worker._seen_outputs
    if accepted:
        worker._seen_outputs.add(output_fingerprint)
    decision_progress = worker._capture_decision_progress(decision) if accepted else False
    progress = accepted and (novel_output or decision_progress)
    report = WorkerReport(
        step=step,
        action="run",
        status="timeout" if timed_out else "ok" if accepted else "failed",
        argv=list(argv),
        command=command,
        command_fingerprint=fingerprint,
        output_fingerprint=output_fingerprint,
        execution_receipt=execution_receipt,
        exit_code=exit_code,
        timed_out=timed_out,
        stdout_artifact=str(stdout_path),
        stderr_artifact=str(stderr_path),
        metadata_artifact=str(metadata_path),
        facts=decision.facts,
        flag_candidates=decision.flag_candidates,
        made_progress=progress,
        redacted=stdout.redacted or stderr.redacted,
        sanitizer_findings=merge_findings(stdout.findings, stderr.findings),
    )
    worker._emit(
        "worker.command",
        {
            "argv": list(argv),
            "exit_code": exit_code,
            "timed_out": timed_out,
            "accepted": accepted,
            "fingerprint": fingerprint,
        },
    )
    return report


def _container_user() -> str:
    if os.name != "posix":
        return "10001:10001"
    uid = os.getuid()
    gid = os.getgid()
    if uid == 0:
        return "10001:10001"
    return f"{uid}:{gid}"
