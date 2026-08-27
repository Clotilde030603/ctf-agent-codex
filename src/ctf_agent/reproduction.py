"""Clean-environment solver replay with resource-conscious Docker execution."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path


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
    image: str = "python:3.12-slim",
    timeout_seconds: float = 120,
    use_docker: bool = True,
) -> ReplayResult:
    solve = run_dir / "solve.py"
    if not solve.is_file():
        return ReplayResult(False, "", "solve.py is missing", 127, [])
    if use_docker and shutil.which("docker"):
        command = [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--cpus=1",
            "--memory=512m",
            "--pids-limit=128",
            "--read-only",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
            f"--mount=type=bind,src={run_dir.resolve()},dst=/work,readonly",
            "-w",
            "/work",
            image,
            "python",
            "solve.py",
        ]
    else:
        command = ["python3", "-I", str(solve)]
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
    if use_docker and command and command[0] == "docker" and returncode in {125, 126, 127}:
        return await reproduce_solver(
            run_dir,
            expected_flag,
            image=image,
            timeout_seconds=timeout_seconds,
            use_docker=False,
        )
    return ReplayResult(
        returncode == 0 and expected_flag in stdout,
        stdout,
        stderr,
        returncode,
        command,
    )
