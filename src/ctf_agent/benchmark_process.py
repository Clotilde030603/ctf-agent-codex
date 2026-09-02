"""Isolated benchmark command and replay execution."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from ctf_agent.benchmark_helpers import _under
from ctf_agent.benchmark_manifest import BenchmarkChallenge
from ctf_agent.benchmark_models import CommandRecord
from ctf_agent.benchmark_safety import _matches_expected


async def _run_clean_replay(
    manifest: Path,
    challenge: BenchmarkChallenge,
    *,
    timeout_seconds: float,
) -> tuple[CommandRecord | None, bool | None, bool, str | None]:
    replay_command = challenge.replay_command or challenge.command
    fixture_root = _fixture_root(manifest, challenge)
    if challenge.clean_mode == "docker":
        if shutil.which("docker") is None:
            return None, None, True, "docker unavailable"
        with _fresh_workdir(fixture_root) as run_dir:
            command_error = _command_path_error(replay_command, run_dir)
            if command_error is not None:
                raise ValueError(command_error)
            docker_command = [
                "docker",
                "run",
                "--rm",
                "--network=none",
                "--cpus=1",
                "--memory=512m",
                "--pids-limit=128",
                "--read-only",
                "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
                f"--mount=type=bind,src={run_dir},dst=/work,readonly",
                "-w",
                "/work",
                challenge.docker_image,
                *replay_command,
            ]
            record = await _run_command(docker_command, run_dir, timeout_seconds=timeout_seconds)
    else:
        with _fresh_workdir(fixture_root) as run_dir:
            command_error = _command_path_error(replay_command, run_dir)
            if command_error is not None:
                raise ValueError(command_error)
            record = await _run_command(replay_command, run_dir, timeout_seconds=timeout_seconds)
    output = f"{record.stdout}\n{record.stderr}"
    success = (
        record.exit_code == 0 and _matches_expected(output, challenge) and not record.timed_out
    )
    return record, success, False, None


async def _run_command(
    command: list[str],
    cwd: Path,
    *,
    timeout_seconds: float,
    extra_env: Mapping[str, str] | None = None,
) -> CommandRecord:
    started = time.monotonic()
    environment = os.environ.copy()
    environment.update(extra_env or {})
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=environment,
        start_new_session=os.name == "posix",
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_seconds)
    except TimeoutError:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        stdout, stderr = await process.communicate()
        return CommandRecord(
            command=command,
            exit_code=124,
            stdout=stdout.decode(errors="replace"),
            stderr=(stderr.decode(errors="replace") + "\ntimed out").strip(),
            seconds=time.monotonic() - started,
            timed_out=True,
        )
    returncode = process.returncode
    if returncode is None:
        raise RuntimeError("process finished without a return code")
    return CommandRecord(
        command=command,
        exit_code=returncode,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace")[-1000:],
        seconds=time.monotonic() - started,
    )


def _fixture_root(manifest: Path, challenge: BenchmarkChallenge) -> Path:
    root = manifest.parent.resolve()
    fixture = (root / challenge.workdir).resolve()
    if not _under(fixture, root) or not fixture.is_dir():
        raise ValueError("benchmark workdir must be an existing directory under manifest root")
    return fixture


def _command_path_error(command: list[str], run_dir: Path) -> str | None:
    if not command:
        return None
    interpreter_names = {
        "python",
        "python3",
        "node",
        "ruby",
        "perl",
        "sh",
        "bash",
    }
    source_suffixes = {".py", ".sh", ".js", ".mjs", ".rb", ".pl"}
    executable = Path(command[0])
    executable_name = executable.name
    if executable_name in interpreter_names:
        if _has_inline_interpreter_flag(executable_name, command[1:]):
            return "inline interpreter execution is not allowed in benchmark commands"
        script_paths = [
            (run_dir / item).resolve()
            for item in command[1:]
            if not item.startswith("-") and Path(item).suffix in source_suffixes
        ]
        if not script_paths:
            return "benchmark interpreter command must reference a fixture script"
        if any(not _under(path, run_dir) or not path.is_file() for path in script_paths):
            return "benchmark interpreter script must exist inside fresh workdir"
    else:
        resolved_executable = (
            executable.resolve() if executable.is_absolute() else (run_dir / executable).resolve()
        )
        if (
            not _under(resolved_executable, run_dir)
            or not resolved_executable.is_file()
            or resolved_executable.suffix not in source_suffixes
        ):
            return "benchmark executable must be an allowlisted interpreter or fixture script"
    for index, item in enumerate(command):
        path = Path(item)
        if index == 0 and path.is_absolute():
            if path.name in interpreter_names:
                continue
            return f"absolute benchmark executable is not allowlisted: {item}"
        if path.is_absolute() and not _under(path.resolve(), run_dir):
            return f"benchmark command path escapes fresh workdir: {item}"
        if not path.is_absolute() and ".." in path.parts:
            return f"benchmark command path contains parent traversal: {item}"
    return None


def _has_inline_interpreter_flag(executable: str, arguments: list[str]) -> bool:
    for argument in arguments:
        if argument == "-":
            return True
        if executable in {"sh", "bash"} and argument.startswith("-"):
            if "c" in argument[1:]:
                return True
        elif executable in {"python", "python3"}:
            if argument == "-m" or (argument.startswith("-") and "c" in argument[1:]):
                return True
        elif executable == "node" and argument in {"-e", "--eval", "-p", "--print"}:
            return True
        elif executable in {"ruby", "perl"} and argument.startswith("-"):
            if "e" in argument[1:]:
                return True
    return False


@contextmanager
def _fresh_workdir(source: Path) -> Iterator[Path]:
    source = source.resolve()
    with tempfile.TemporaryDirectory(prefix="ctf-benchmark-") as temp_dir:
        target = Path(temp_dir)
        shutil.copytree(
            source,
            target,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        yield target
