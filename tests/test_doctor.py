from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ctf_agent.capabilities import (
    CapabilityProvider,
    ContainerProbeResult,
    StaticCapabilityProbe,
    ToolProbeResult,
)
from ctf_agent.capability_manifest import DEFAULT_CAPABILITY_MANIFEST
from ctf_agent.capability_probe import DockerCapabilityProbe
from ctf_agent.config import Settings
from ctf_agent.doctor import REQUIRED_CONTAINER_TOOLS, run_doctor


def _snapshot(*, reachable: bool):
    reason = None if reachable else "Cannot connect to daemon"
    return CapabilityProvider(
        DEFAULT_CAPABILITY_MANIFEST,
        StaticCapabilityProbe(
            ContainerProbeResult(
                reachable=reachable,
                image_digest="sha256:image" if reachable else None,
                reason=reason,
                tools=tuple(
                    ToolProbeResult(
                        name=item.name,
                        installed=reachable,
                        reachable=reachable,
                        authenticated=True if reachable else None,
                        version="test-1" if reachable else None,
                        reason=reason,
                    )
                    for item in DEFAULT_CAPABILITY_MANIFEST.capabilities
                ),
            )
        ),
    ).snapshot("test-tools:1")


def test_doctor_rejects_docker_cli_without_daemon(tmp_path: Path) -> None:
    report = run_doctor(
        Settings(backend="static", runs_dir=tmp_path / "runs"),
        capability_snapshot=_snapshot(reachable=False),
    )

    image = next(check for check in report.checks if check.name == "ctf-tool-image")
    assert image.status == "error"
    assert "Cannot connect" in image.detail
    assert report.ok is False


def test_container_probe_is_non_root_offline_and_resource_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(
        command: list[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        commands.append(command)
        if "inspect" in command:
            return subprocess.CompletedProcess(command, 0, "sha256:image\n", "")
        output = "\n".join(
            f"{item.name}\t1\ttest-1"
            for item in DEFAULT_CAPABILITY_MANIFEST.capabilities
        )
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr("ctf_agent.capability_probe.shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr("ctf_agent.capability_probe._run", fake_run)

    snapshot = CapabilityProvider(
        DEFAULT_CAPABILITY_MANIFEST, DockerCapabilityProbe()
    ).snapshot("test-tools:1")

    assert snapshot.image_digest == "sha256:image"
    smoke = next(command for command in commands if "run" in command)
    assert "--network=none" in smoke
    assert "--read-only" in smoke
    assert "--pids-limit=64" in smoke
    assert "--memory=256m" in smoke
    assert "--cpus=1" in smoke
    script = smoke[-1]
    assert all(f"command -v {tool}" in script for tool in REQUIRED_CONTAINER_TOOLS)


def test_doctor_accepts_complete_required_container_manifest(tmp_path: Path) -> None:
    report = run_doctor(
        Settings(backend="static", runs_dir=tmp_path / "runs"),
        capability_snapshot=_snapshot(reachable=True),
    )

    assert report.ok is True
    assert all(
        next(check for check in report.checks if check.name == f"capability:{tool}").status
        != "error"
        for tool in REQUIRED_CONTAINER_TOOLS
    )


def test_ctf_tool_dockerfile_pins_base_and_direct_packages() -> None:
    dockerfile = (
        Path(__file__).parents[1] / "docker" / "ctf-tools" / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert "python:3.12.11-slim-bookworm@sha256:" in dockerfile
    for package in (
        "binutils=",
        "binwalk=",
        "checksec=",
        "file=",
        "foremost=",
        "libimage-exiftool-perl=",
        "tini=",
        "tshark=",
    ):
        assert package in dockerfile
