from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import click
from typer.main import get_command
from typer.testing import CliRunner

from ctf_agent.cli import app
from ctf_agent.config import Settings

ROOT = Path(__file__).parents[1]


def test_bilingual_readmes_have_user_guide_sections_and_valid_local_links() -> None:
    required = {
        "README.md": (
            "## What is CTF Agent Codex?",
            "## Key Features",
            "## How It Works",
            "## Current Project Status",
            "## Installation",
            "## Codex Setup",
            "## First-Time Authentication",
            "## Quick Start",
            "## Troubleshooting",
            "## Contributing",
            "## Disclaimer",
        ),
        "README.ko.md": (
            "## CTF Agent Codex란?",
            "## 주요 기능",
            "## 동작 방식",
            "## 현재 프로젝트 상태",
            "## 설치",
            "## Codex 설정",
            "## 최초 CTF 플랫폼 인증",
            "## Quick Start",
            "## 문제 해결",
            "## 기여",
            "## 면책 고지",
        ),
    }
    for filename, headings in required.items():
        text = (ROOT / filename).read_text(encoding="utf-8")
        assert all(heading in text for heading in headings)
        assert "```mermaid\nflowchart LR" in text
        for link in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
            if "://" in link or link.startswith(("#", "mailto:")):
                continue
            assert (ROOT / link.split("#", 1)[0]).exists(), (filename, link)


def test_readme_cli_examples_match_typer_help() -> None:
    runner = CliRunner()
    expected = {
        "solve": (
            "--auto-submit",
            "--dry-run",
            "--backend",
            "--planner-model",
            "--solver-model",
            "--reviewer-model",
            "--reasoning-effort",
            "--max-workers",
            "--writeup",
            "--no-writeup",
            "--runs-dir",
            "--allow-private-host",
            "--allow-local-reproduction",
        ),
        "resume": ("--runs-dir", "--challenge-url"),
        "benchmark": (),
    }
    root_command = get_command(app)
    assert isinstance(root_command, click.Group)
    context = click.Context(root_command)
    for command, options in expected.items():
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        subcommand = root_command.get_command(context, command)
        assert subcommand is not None
        declared_options = {
            option
            for parameter in subcommand.params
            if isinstance(parameter, click.Option)
            for option in (*parameter.opts, *parameter.secondary_opts)
        }
        assert set(options) <= declared_options

    conflict = runner.invoke(
        app,
        ["solve", "https://ctf.test/challenges/1", "--auto-submit", "--dry-run"],
    )
    assert conflict.exit_code != 0
    assert "mutually exclusive" in (conflict.stdout + conflict.stderr)

    for filename in ("README.md", "README.ko.md"):
        text = (ROOT / filename).read_text(encoding="utf-8")
        assert "ctf-agent status" not in text


def test_env_example_uses_real_settings_names() -> None:
    settings = Settings()
    setting_defaults = {
        f"CTF_{name.upper()}": _format_env_value(getattr(settings, name))
        for name in Settings.model_fields
        if getattr(settings, name) is not None
    }
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    active = dict(
        line.split("=", 1)
        for line in example.splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    assert active == setting_defaults


def test_bilingual_readmes_keep_cli_config_and_limit_claims_in_sync() -> None:
    english = (ROOT / "README.md").read_text(encoding="utf-8")
    korean = (ROOT / "README.ko.md").read_text(encoding="utf-8")
    env_pattern = r"`(CTF_[A-Z_]+)`"
    option_pattern = r"`(--[a-z-]+)`"
    assert set(re.findall(env_pattern, english)) == set(re.findall(env_pattern, korean))
    assert set(re.findall(option_pattern, english)) == set(
        re.findall(option_pattern, korean)
    )
    assert "custom session must be injected in code" in english
    assert "custom session은 코드에서 주입" in korean
    assert "does **not** perform a separate clean-environment replay" in english
    assert "별도의 clean-environment replay를 수행하지는 않습니다" in korean
    assert "not individual archive-member extraction" in english
    assert "archive member별 extraction 한도가 아닙니다" in korean


def _format_env_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
