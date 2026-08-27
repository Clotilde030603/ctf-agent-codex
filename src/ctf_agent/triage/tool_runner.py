from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from .types import ToolRunResult, path_to_text


def run_tool(
    command: list[str],
    artifacts_dir: Path,
    *,
    timeout_seconds: float = 5.0,
    name: str | None = None,
    input_bytes: bytes | None = None,
) -> ToolRunResult:
    tool_name = name or command[0]
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    if not command or shutil.which(command[0]) is None:
        return ToolRunResult(
            tool=tool_name,
            command=command,
            exit_code=None,
            timed_out=False,
            duration_seconds=0.0,
            missing=True,
            error=f"tool not found: {command[0] if command else '<empty>'}",
        )

    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            input=input_bytes,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        timed_out = False
        exit_code: int | None = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
    duration = time.monotonic() - started

    safe_name = "".join(char if char.isalnum() or char in "._-" else "_" for char in tool_name)
    stamp = f"{int(started * 1000)}"
    stdout_path = artifacts_dir / f"{safe_name}-{stamp}.stdout"
    stderr_path = artifacts_dir / f"{safe_name}-{stamp}.stderr"
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)

    return ToolRunResult(
        tool=tool_name,
        command=command,
        exit_code=exit_code,
        timed_out=timed_out,
        duration_seconds=round(duration, 6),
        stdout_artifact=path_to_text(stdout_path),
        stderr_artifact=path_to_text(stderr_path),
    )
