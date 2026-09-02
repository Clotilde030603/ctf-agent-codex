"""Operational preflight checks for the local CTF agent runtime."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ctf_agent.capabilities import (
    CapabilityCategory,
    CapabilityStatus,
    RuntimeCapabilitySnapshot,
    default_capability_provider,
)
from ctf_agent.capability_manifest import DEFAULT_CAPABILITY_MANIFEST
from ctf_agent.config import Settings

REQUIRED_CONTAINER_TOOLS = tuple(
    item.name
    for item in DEFAULT_CAPABILITY_MANIFEST.capabilities
    if item.category is CapabilityCategory.TOOL and item.required
)


class DoctorCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    status: str = Field(pattern=r"^(ok|warning|error)$")
    detail: str
    remediation: str | None = None


class DoctorReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    checks: tuple[DoctorCheck, ...]
    backend: str
    planner_model: str
    solver_model: str
    reviewer_model: str
    docker_image: str
    runtime_capabilities: RuntimeCapabilitySnapshot

    @property
    def ok(self) -> bool:
        return not any(check.status == "error" for check in self.checks)


def run_doctor(
    settings: Settings,
    *,
    capability_snapshot: RuntimeCapabilitySnapshot | None = None,
) -> DoctorReport:
    snapshot = capability_snapshot or default_capability_provider().snapshot(
        settings.docker_image
    )
    checks = [
        _python_check(),
        _runs_directory_check(settings.runs_dir),
        *_codex_checks(settings),
        *_docker_checks(snapshot),
        *_capability_checks(snapshot),
        DoctorCheck(
            name="configuration",
            status="ok",
            detail=(
                f"backend={settings.backend}; planner={settings.planner_model}/"
                f"{settings.planner_effort}; solver={settings.solver_model}/"
                f"{settings.solver_effort}; reviewer={settings.verifier_model}/"
                f"{settings.verifier_effort}"
            ),
        ),
    ]
    return DoctorReport(
        checks=tuple(checks),
        backend=settings.backend,
        planner_model=settings.planner_model,
        solver_model=settings.solver_model,
        reviewer_model=settings.verifier_model,
        docker_image=settings.docker_image,
        runtime_capabilities=snapshot,
    )


def _python_check() -> DoctorCheck:
    version = sys.version_info
    ready = version >= (3, 12)
    return DoctorCheck(
        name="python",
        status="ok" if ready else "error",
        detail=f"{version.major}.{version.minor}.{version.micro}",
        remediation=None if ready else "Install Python 3.12 or newer.",
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


def _codex_checks(settings: Settings) -> tuple[DoctorCheck, ...]:
    executable = shutil.which(settings.codex_binary)
    if settings.backend != "codex":
        return (
            DoctorCheck(
                name="codex-cli",
                status="warning",
                detail="not required for static backend",
            ),
        )
    if executable is None:
        return (
            DoctorCheck(
                name="codex-cli",
                status="error",
                detail=f"executable not found: {settings.codex_binary}",
                remediation="Install and authenticate the Codex CLI.",
            ),
        )
    status = _run_command([executable, "login", "status"], timeout=15)
    authenticated = status.returncode == 0
    return (
        DoctorCheck(name="codex-cli", status="ok", detail=executable),
        DoctorCheck(
            name="codex-authentication",
            status="ok" if authenticated else "error",
            detail=(status.stdout or status.stderr or "no status output").strip()[:1000],
            remediation=None if authenticated else "Run `codex` and sign in.",
        ),
    )


def _docker_checks(snapshot: RuntimeCapabilitySnapshot) -> tuple[DoctorCheck, ...]:
    reachable = snapshot.image_digest is not None
    detail = snapshot.image_digest or snapshot.probe_reason or "Docker image is unreachable"
    return (
        DoctorCheck(
            name="ctf-tool-image",
            status="ok" if reachable else "error",
            detail=detail,
            remediation=(
                None
                if reachable
                else "Start Docker and build the configured docker/ctf-tools image."
            ),
        ),
    )


def _capability_checks(
    snapshot: RuntimeCapabilitySnapshot,
) -> tuple[DoctorCheck, ...]:
    checks: list[DoctorCheck] = []
    for capability in snapshot.capabilities:
        status = "ok"
        if capability.status is not CapabilityStatus.AVAILABLE:
            status = "error" if capability.required and capability.status in {
                CapabilityStatus.MISSING,
                CapabilityStatus.UNREACHABLE,
                CapabilityStatus.UNAUTHENTICATED,
                CapabilityStatus.MISCONFIGURED,
            } else "warning"
        checks.append(
            DoctorCheck(
                name=f"capability:{capability.name}",
                status=status,
                detail=(
                    f"status={capability.status.value}; version={capability.version or 'unknown'}; "
                    f"source={capability.source}; reason={capability.reason}"
                ),
                remediation=(
                    "Rebuild the configured Docker image from docker/ctf-tools/Dockerfile."
                    if status == "error"
                    else None
                ),
            )
        )
    return tuple(checks)



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
