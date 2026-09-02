"""Typed clean-environment solver reproduction."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from ctf_agent.config import DEFAULT_CTF_TOOL_IMAGE
from ctf_agent.solver_command import canonical_network_host


class NetworkCapability(StrEnum):
    UNAVAILABLE = "unavailable"


class ReproductionSpec(BaseModel):
    """Exact controller-validated solver invocation without secret values."""

    model_config = ConfigDict(frozen=True)

    argv: tuple[str, ...]
    cwd: Path
    env_keys: tuple[str, ...] = ()
    solver_path: Path
    network: NetworkCapability = NetworkCapability.UNAVAILABLE
    requires_auth_handle: bool = False

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item or "\x00" in item for item in value):
            raise ValueError("argv entries must be non-empty and NUL-free")
        if Path(value[0]).name in {"sh", "bash", "zsh", "fish", "cmd", "powershell", "pwsh"}:
            raise ValueError("shell executables are not allowed")
        canonical_network_host(value)
        return value

    @field_validator("env_keys")
    @classmethod
    def reject_environment_selection(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value:
            raise ValueError("reproduction environment is controller-owned")
        return value

    @model_validator(mode="after")
    def validate_solver(self) -> ReproductionSpec:
        if self.solver_path.name != "solve.py":
            raise ValueError("solver_path must name solve.py")
        if self.network is not NetworkCapability.UNAVAILABLE:
            raise ValueError("reproduction network must be unavailable")
        return self


@dataclass(frozen=True, slots=True)
class ReplayResult:
    success: bool
    stdout: str
    stderr: str
    exit_code: int
    command: list[str]


def controller_reproduction_spec(
    run_root: Path,
    workspace: Path,
    argv: tuple[str, ...],
    *,
    requires_auth_handle: bool = False,
) -> ReproductionSpec:
    """Build a reproduction spec solely from controller-owned paths and report argv."""
    root = run_root.resolve(strict=True)
    cwd = workspace.resolve(strict=True)
    if cwd != root and root not in cwd.parents:
        raise ValueError("reproduction cwd must be beneath the run root")
    if _contains_symlink(workspace, run_root):
        raise ValueError("reproduction cwd must not contain symlinks")
    solver = workspace / "solve.py"
    if solver.is_symlink():
        raise ValueError("reproduction solver must not be a symlink")
    resolved_solver = solver.resolve(strict=True)
    if resolved_solver != cwd / "solve.py" or root not in resolved_solver.parents:
        raise ValueError("reproduction solver must be the workspace solve.py")
    _validate_solver_argv(argv, cwd, resolved_solver)
    return ReproductionSpec(
        argv=argv,
        cwd=cwd,
        solver_path=resolved_solver,
        network=NetworkCapability.UNAVAILABLE,
        requires_auth_handle=requires_auth_handle,
    )


async def reproduce_solver(
    run_dir: Path,
    expected_flag: str,
    *,
    spec: ReproductionSpec | None = None,
    image: str = DEFAULT_CTF_TOOL_IMAGE,
    timeout_seconds: float = 120,
    use_docker: bool = True,
) -> ReplayResult:
    try:
        active_spec = spec or _default_spec(run_dir)
        active_spec = controller_reproduction_spec(
            run_dir,
            run_dir,
            active_spec.argv,
            requires_auth_handle=active_spec.requires_auth_handle,
        )
        if spec is not None and (
            spec.cwd != active_spec.cwd or spec.solver_path != active_spec.solver_path
        ):
            return ReplayResult(False, "", "non-canonical reproduction cwd or solver", 126, [])
    except (FileNotFoundError, ValueError) as exc:
        return ReplayResult(False, "", str(exc), 126, [])
    if use_docker and shutil.which("docker") is not None:
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
            f"--mount=type=bind,src={active_spec.cwd},dst=/work,readonly",
            "-w",
            "/work",
            image,
            *active_spec.argv,
        ]
        execution_command = command
    elif not use_docker:
        namespace_prefix = await _network_namespace_prefix()
        if namespace_prefix is None:
            return ReplayResult(False, "", "host network isolation is unavailable", 127, [])
        command = list(active_spec.argv)
        execution_command = [*namespace_prefix, *command]
    else:
        return ReplayResult(False, "", "Docker is unavailable", 127, ["docker"])
    process = await asyncio.create_subprocess_exec(
        *execution_command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=active_spec.cwd,
        env=_reproduction_env(),
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
    if not use_docker and returncode != 0 and stderr.startswith("unshare:"):
        return ReplayResult(False, stdout, "host network isolation is unavailable", 127, command)
    return ReplayResult(
        returncode == 0 and expected_flag in (stdout + "\n" + stderr),
        stdout,
        stderr,
        returncode,
        command,
    )



async def _network_namespace_prefix() -> tuple[str, ...] | None:
    unshare = shutil.which("unshare")
    if unshare is None:
        return None
    prefix = (unshare, "--user", "--map-root-user", "--net", "--")
    probe = await asyncio.create_subprocess_exec(
        *prefix,
        "true",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    if await probe.wait() != 0:
        return None
    return prefix

def _default_spec(run_dir: Path) -> ReproductionSpec:
    return controller_reproduction_spec(
        run_dir,
        run_dir,
        ("python3", "-I", "solve.py"),
    )


def _contains_symlink(path: Path, boundary: Path) -> bool:
    current = path.absolute()
    stop = boundary.absolute().parent
    while current != stop:
        if current.is_symlink():
            return True
        if current.parent == current:
            return False
        current = current.parent
    return False


def _validate_solver_argv(argv: tuple[str, ...], cwd: Path, solver: Path) -> None:
    solver_arguments = [Path(item) for item in argv if Path(item).name == "solve.py"]
    if len(solver_arguments) != 1:
        raise ValueError("argv must contain exactly one solve.py")
    argument = solver_arguments[0]
    invoked_solver = argument if argument.is_absolute() else cwd / argument
    if invoked_solver.is_symlink() or invoked_solver.resolve() != solver:
        raise ValueError("argv must invoke the canonical workspace solve.py")


def _reproduction_env() -> dict[str, str]:
    allowed = {"LANG", "LC_ALL", "PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR"}
    return {
        key: value
        for key, value in os.environ.items()
        if key in allowed
    } | {"PYTHONUNBUFFERED": "1"}


def main() -> int:
    arguments = sys.argv[1:]
    if len(arguments) < 2 or arguments[0] != "--solver":
        print(json.dumps({"status": "invalid", "reason": "usage: --solver PATH -- [ARGS...]"}))
        return 2
    solver = Path(arguments[1]).resolve()
    trailing = arguments[2:]
    if trailing[:1] == ["--"]:
        trailing = trailing[1:]
    try:
        spec = controller_reproduction_spec(
            solver.parent,
            solver.parent,
            ("python3", solver.name, *trailing),
        )
    except (FileNotFoundError, ValueError) as exc:
        print(json.dumps({"status": "invalid", "reason": str(exc)}))
        return 2
    if "--host" in trailing or "--port" in trailing:
        print(json.dumps({"status": "network_unavailable", "argv": list(spec.argv)}))
        return 3
    result = asyncio.run(reproduce_solver(solver.parent, "", spec=spec, use_docker=False))
    if result.exit_code == 127:
        print(
            json.dumps(
                {
                    "status": "reproduction_unavailable",
                    "argv": list(spec.argv),
                    "reason": result.stderr,
                }
            )
        )
        return 4
    print(
        json.dumps(
            {
                "status": "ok" if result.success else "failed",
                "argv": list(spec.argv),
                "exit_code": result.exit_code,
            }
        )
    )
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
