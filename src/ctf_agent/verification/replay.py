"""Fresh subprocess replay for solver reproducibility."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ReplayResult:
    command: tuple[str, ...]
    cwd: Path
    returncode: int
    stdout: str
    stderr: str
    matched_flag: str | None

    @property
    def success(self) -> bool:
        return self.returncode == 0 and self.matched_flag is not None


def replay_solver(
    solver_path: Path,
    *,
    expected_flag: str | None = None,
    flag_regex: str | None = None,
    timeout_seconds: float = 30.0,
    cwd: Path | None = None,
    extra_env: Mapping[str, str] | None = None,
    python_executable: str = sys.executable,
    args: Sequence[str] = (),
) -> ReplayResult:
    solver_path = solver_path.resolve()
    run_cwd = (cwd or solver_path.parent).resolve()
    command = (python_executable, str(solver_path), *tuple(args))

    try:
        completed = subprocess.run(
            command,
            cwd=run_cwd,
            env=_fresh_env(extra_env),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return ReplayResult(
            command,
            run_cwd,
            124,
            stdout,
            stderr or f"timed out after {timeout_seconds} seconds",
            None,
        )

    matched_flag = _extract_flag(
        completed.stdout + "\n" + completed.stderr,
        expected_flag=expected_flag,
        flag_regex=flag_regex,
    )
    return ReplayResult(
        command,
        run_cwd,
        completed.returncode,
        completed.stdout,
        completed.stderr,
        matched_flag,
    )


def _fresh_env(extra_env: Mapping[str, str] | None) -> dict[str, str]:
    allowed = {"LANG", "LC_ALL", "PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "PYTHONPATH"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["PYTHONUNBUFFERED"] = "1"
    if extra_env:
        env.update({str(key): str(value) for key, value in extra_env.items()})
    return env


def _extract_flag(text: str, *, expected_flag: str | None, flag_regex: str | None) -> str | None:
    if expected_flag:
        return expected_flag if expected_flag in text else None
    if not flag_regex:
        return None
    match = re.search(flag_regex, text)
    return match.group(0) if match else None
