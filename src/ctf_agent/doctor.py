"""Operational preflight checks for the local CTF agent runtime."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from ctf_agent.config import Settings

REQUIRED_CONTAINER_TOOLS = (
    "python3",
    "file",
    "strings",
    "objdump",
    "readelf",
    "exiftool",
    "binwalk",
    "checksec",
    "foremost",
    "tshark",
)


class DoctorCheck(BaseModel):
    name: str
    status: str = Field(pattern=r"^(ok|warning|error)$")
    detail: str
    remediation: str | None = None


class DoctorReport(BaseModel):
    checks: list[DoctorCheck]
    backend: str
    planner_model: str
    solver_model: str
    reviewer_model: str
    docker_image: str

    @property
    def ok(self) -> bool:
        return not any(check.status == "error" for check in self.checks)


def run_doctor(settings: Settings) -> DoctorReport:
    checks: list[DoctorCheck] = []
    checks.append(_python_check())
    checks.append(_runs_directory_check(settings.runs_dir))
    checks.extend(_codex_checks(settings))
    checks.extend(_docker_checks(settings))
    checks.append(_playwright_check())
    checks.append(
        DoctorCheck(
            name="configuration",
            status="ok",
            detail=(
                f"backend={settings.backend}; planner={settings.planner_model}/"
                f"{settings.planner_effort}; solver={settings.solver_model}/"
                f"{settings.solver_effort}; reviewer={settings.verifier_model}/"
                f"{settings.verifier_effort}"
            ),
        )
    )
    return DoctorReport(
        checks=checks,
        backend=settings.backend,
        planner_model=settings.planner_model,
        solver_model=settings.solver_model,
        reviewer_model=settings.verifier_model,
        docker_image=settings.docker_image,
    )


def _python_check() -> DoctorCheck:
    version = sys.version_info
    status = "ok" if version >= (3, 12) else "error"
    return DoctorCheck(
        name="python",
        status=status,
        detail=f"{version.major}.{version.minor}.{version.micro}",
        remediation="Install Python 3.12 or newer." if status == "error" else None,
    )


def _runs_directory_check(runs_dir: Path) -> DoctorCheck:
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        probe = runs_dir / f".doctor-{uuid4().hex}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return DoctorCheck(
            name="runs-directory",
            status="error",
            detail=str(exc),
            remediation=f"Make {runs_dir} writable by the current user.",
        )
    return DoctorCheck(name="runs-directory", status="ok", detail=str(runs_dir.resolve()))


def _codex_checks(settings: Settings) -> list[DoctorCheck]:
    executable = shutil.which(settings.codex_binary)
    if settings.backend != "codex":
        return [
            DoctorCheck(
                name="codex-cli",
                status="warning",
                detail="not required for static backend",
            )
        ]
    if executable is None:
        return [
            DoctorCheck(
                name="codex-cli",
                status="error",
                detail=f"executable not found: {settings.codex_binary}",
                remediation="Install and authenticate the Codex CLI.",
            )
        ]
    status = _run_command([executable, "login", "status"], timeout=15)
    return [
        DoctorCheck(name="codex-cli", status="ok", detail=executable),
        DoctorCheck(
            name="codex-authentication",
            status="ok" if status.returncode == 0 else "error",
            detail=(status.stdout or status.stderr or "no status output").strip()[:1000],
            remediation=(
                "Run `codex` and sign in before using backend=codex."
                if status.returncode != 0
                else None
            ),
        ),
    ]


def _docker_checks(settings: Settings) -> list[DoctorCheck]:
    executable = shutil.which("docker")
    if executable is None:
        return [
            DoctorCheck(
                name="docker-cli",
                status="error",
                detail="docker executable not found",
                remediation="Install Docker and start its daemon.",
            )
        ]
    checks = [DoctorCheck(name="docker-cli", status="ok", detail=executable)]
    daemon = _run_command([executable, "info", "--format", "{{.ServerVersion}}"])
    daemon_detail = (daemon.stderr or daemon.stdout or "").strip()
    daemon_ready = (
        daemon.returncode == 0
        and bool(daemon.stdout.strip())
        and "cannot connect" not in daemon_detail.casefold()
    )
    if not daemon_ready:
        checks.append(
            DoctorCheck(
                name="docker-daemon",
                status="error",
                detail=daemon_detail or "daemon did not return a server version",
                remediation="Start the Docker daemon and rerun `ctf-agent doctor`.",
            )
        )
        return checks
    checks.append(
        DoctorCheck(name="docker-daemon", status="ok", detail=daemon.stdout.strip())
    )
    image = _run_command([executable, "image", "inspect", settings.docker_image])
    if image.returncode != 0:
        checks.append(
            DoctorCheck(
                name="ctf-tool-image",
                status="error",
                detail=f"image is not available: {settings.docker_image}",
                remediation=(
                    "docker build -t "
                    f"{settings.docker_image} -f docker/ctf-tools/Dockerfile ."
                ),
            )
        )
        return checks
    checks.append(
        DoctorCheck(name="ctf-tool-image", status="ok", detail=settings.docker_image)
    )
    command = [
        executable,
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--pids-limit=64",
        "--memory=256m",
        "--cpus=1",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
        settings.docker_image,
        "sh",
        "-c",
        "test \"$(id -u)\" != 0 && "
        + " && ".join(f"command -v {tool} >/dev/null" for tool in REQUIRED_CONTAINER_TOOLS),
    ]
    tools = _run_command(command, timeout=60)
    checks.append(
        DoctorCheck(
            name="ctf-tool-smoke",
            status="ok" if tools.returncode == 0 else "error",
            detail=(tools.stdout or tools.stderr or "tool smoke completed").strip()[:1000],
            remediation=(
                "Rebuild docker/ctf-tools/Dockerfile and inspect missing commands."
                if tools.returncode != 0
                else None
            ),
        )
    )
    return checks


def _playwright_check() -> DoctorCheck:
    if importlib.util.find_spec("playwright") is None:
        return DoctorCheck(
            name="playwright-chromium",
            status="warning",
            detail="playwright package is not installed",
            remediation='Install `.[browser]` and run `playwright install chromium`.',
        )
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
    except Exception as exc:
        return DoctorCheck(
            name="playwright-chromium",
            status="error",
            detail=f"Playwright inspection failed: {exc}",
            remediation="Run `playwright install chromium`.",
        )
    return DoctorCheck(
        name="playwright-chromium",
        status="ok" if executable.is_file() else "error",
        detail=str(executable),
        remediation=("Run `playwright install chromium`." if not executable.is_file() else None),
    )


def _run_command(
    command: list[str], *, timeout: float = 20
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            env={"PATH": os.environ.get("PATH", "")},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 124, "", str(exc))
