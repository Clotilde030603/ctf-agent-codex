"""Clean-environment solver replay with resource-conscious Docker execution."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

from ctf_agent.config import DEFAULT_CTF_TOOL_IMAGE


@dataclass(frozen=True, slots=True)
class ReplayResult:
    success: bool
    stdout: str
    stderr: str
    exit_code: int
    command: list[str]


async def reproduce_solver(
    run_dir: Path,
    expected_flag: str,
    *,
    image: str = DEFAULT_CTF_TOOL_IMAGE,
    timeout_seconds: float = 120,
    use_docker: bool = True,
) -> ReplayResult:
    solve = run_dir / "solve.py"
    if not solve.is_file():
        return ReplayResult(False, "", "solve.py is missing", 127, [])
    docker_available = shutil.which("docker") is not None
    if use_docker and docker_available:
        command = [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--cpus=1",
            "--memory=512m",
            "--pids-limit=128",
            "--user=10001:10001",
            "--read-only",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
            f"--mount=type=bind,src={run_dir.resolve()},dst=/work,readonly",
            "-w",
            "/work",
            image,
            "python",
            "solve.py",
        ]
    elif not use_docker:
        command = ["python3", "-I", str(solve)]
    else:
        return ReplayResult(False, "", "Docker is unavailable", 127, ["docker"])
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=run_dir,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout_seconds
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        return ReplayResult(False, "", "reproduction timed out", 124, command)
    stdout = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace")
    returncode = process.returncode
    if returncode is None:
        raise RuntimeError("solver process ended without an exit code")
    return ReplayResult(
        returncode == 0 and expected_flag in (stdout + "\n" + stderr),
        stdout,
        stderr,
        returncode,
        command,
    )
