from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ctf_agent.config import Settings
from ctf_agent.doctor import REQUIRED_CONTAINER_TOOLS, run_doctor


def completed(
    command: list[str], code: int = 0, output: str = "ok"
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        command,
        code,
        output if code == 0 else "",
        output if code else "",
    )


def test_doctor_rejects_docker_cli_without_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ctf_agent.doctor.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        "ctf_agent.doctor.importlib.util.find_spec", lambda _name: None
    )

    def fake_run(command: list[str], *, timeout: float = 20) -> subprocess.CompletedProcess[str]:
        assert command[1:3] == ["info", "--format"]
        return subprocess.CompletedProcess(command, 0, "", "Cannot connect to daemon")

    monkeypatch.setattr("ctf_agent.doctor._run_command", fake_run)

    report = run_doctor(Settings(backend="static", runs_dir=tmp_path / "runs"))

    daemon = next(check for check in report.checks if check.name == "docker-daemon")
    assert daemon.status == "error"
    assert report.ok is False


def test_doctor_tool_smoke_is_non_root_offline_and_resource_limited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ctf_agent.doctor.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        "ctf_agent.doctor.importlib.util.find_spec", lambda _name: None
    )
    commands: list[list[str]] = []

    def fake_run(command: list[str], *, timeout: float = 20) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return completed(command, output="24.0")

    monkeypatch.setattr("ctf_agent.doctor._run_command", fake_run)

    report = run_doctor(Settings(backend="static", runs_dir=tmp_path / "runs"))

    assert report.ok is True
    smoke = next(command for command in commands if "run" in command)
    assert "--network=none" in smoke
    assert "--read-only" in smoke
    assert "--pids-limit=64" in smoke
    assert "--memory=256m" in smoke
    assert "--cpus=1" in smoke
    script = smoke[-1]
    assert 'test "$(id -u)" != 0' in script
    assert all(f"command -v {tool}" in script for tool in REQUIRED_CONTAINER_TOOLS)


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
