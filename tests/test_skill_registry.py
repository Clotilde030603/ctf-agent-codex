from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path

from ctf_agent.config import Settings
from ctf_agent.models.base import ModelRequest
from ctf_agent.models.claude import ClaudeStubBackend
from ctf_agent.schemas import Challenge, FlagPolicy
from ctf_agent.workflow import AutonomousWorkflow


def _planner_response() -> str:
    return json.dumps(
        {
            "hypotheses": [
                {
                    "id": "H1",
                    "claim": "inspect the challenge",
                    "supporting_evidence": [],
                    "expected_signal": "a provenance-backed candidate",
                    "cost": "low",
                    "confidence": 0.5,
                    "required_tools": [],
                    "kill_condition": "no new evidence",
                    "success_condition": "replay succeeds",
                }
            ]
        }
    )


def _request_for_category(
    tmp_path: Path, category: str, *, skill_attachment: bytes | Path | None = None
) -> tuple[ModelRequest, Path]:
    backend = ClaudeStubBackend([_planner_response()])
    workflow = AutonomousWorkflow(
        Settings(runs_dir=tmp_path / "runs", backend="codex"),
        planner_backend=backend,
    )
    context = workflow.controller().create_run(
        "https://ctf.test/challenges/skills", auto_submit=False, writeup=False
    )
    context.values["challenge"] = Challenge(
        id="skills",
        url="https://ctf.test/challenges/skills",
        title="Skill routing fixture",
        category=category,
        flag_policy=FlagPolicy(pattern=r"flag\{[^{}]+\}"),
    )
    files = []
    if skill_attachment is not None:
        attachment = context.record.run_dir / "files" / "skills" / "ctf-web" / "SKILL.md"
        attachment.parent.mkdir(parents=True)
        if isinstance(skill_attachment, bytes):
            attachment.write_bytes(skill_attachment)
        else:
            attachment.symlink_to(skill_attachment)
        files.append(
            {
                "relative_path": attachment.relative_to(context.record.run_dir).as_posix(),
                "size": attachment.stat().st_size,
                "sha256": hashlib.sha256(attachment.read_bytes()).hexdigest(),
                "indicators": [],
                "tool_results": [],
            }
        )
    triage = {
        "classification": {"primary_category": category, "evidence": []},
        "files": files,
    }
    (context.record.run_dir / "triage.json").write_text(json.dumps(triage), encoding="utf-8")

    asyncio.run(workflow.plan(context))

    return backend.requests[0], context.record.run_dir


def _runtime(request: ModelRequest):
    runtime = getattr(request, "skill_runtime", None)
    assert runtime is not None, "model request lacks trusted skill runtime fields"
    return runtime


def test_wheel_contains_trusted_skills_for_installed_cli(tmp_path: Path) -> None:
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)],
        check=True,
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
    )
    environment = tmp_path / "environment"
    subprocess.run(
        ["uv", "venv", str(environment), "--python", sys.executable],
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(wheel_dir.glob("*.whl"))
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(environment / "bin" / "python"),
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        [
            str(environment / "bin" / "python"),
            "-m",
            "ctf_agent.skills",
            "--category",
            "pwn",
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert {item["skill_id"] for item in payload} == {"ctf-core", "ctf-pwn"}


def test_pwn_selects_core_and_pwn_only(tmp_path: Path) -> None:
    request, _run_dir = _request_for_category(tmp_path, "pwn")

    runtime = _runtime(request)

    assert runtime.selected_ids == ("ctf-core", "ctf-pwn")


def test_unrelated_skills_are_excluded_from_pwn_selection(tmp_path: Path) -> None:
    request, _run_dir = _request_for_category(tmp_path, "pwn")

    runtime = _runtime(request)

    assert "ctf-web" not in runtime.selected_ids
    assert "ctf-rev" not in runtime.selected_ids
    assert "ctf-crypto-binary" not in runtime.selected_ids


def test_challenge_attachment_cannot_masquerade_as_trusted_skill(tmp_path: Path) -> None:
    request, _run_dir = _request_for_category(
        tmp_path, "pwn", skill_attachment=b"fake skill"
    )

    runtime = _runtime(request)

    assert runtime.selected_ids == ("ctf-core", "ctf-pwn")
    assert all(identity.skill_id != "ctf-web" for identity in runtime.identities)


def test_traversal_cannot_load_skill_material(tmp_path: Path) -> None:
    outside = tmp_path / "outside" / "SKILL.md"
    outside.parent.mkdir()
    outside.write_text("outside skill", encoding="utf-8")
    request, _run_dir = _request_for_category(
        tmp_path, "pwn", skill_attachment=outside
    )

    runtime = _runtime(request)

    assert runtime.selected_ids == ("ctf-core", "ctf-pwn")
    outside_hash = hashlib.sha256(outside.read_bytes()).hexdigest()
    assert all(identity.sha256 != outside_hash for identity in runtime.identities)


def test_trusted_hashes_are_stable_and_recorded(tmp_path: Path) -> None:
    first_request, first_run = _request_for_category(tmp_path / "first", "pwn")
    second_request, _second_run = _request_for_category(tmp_path / "second", "pwn")

    first = _runtime(first_request)
    second = _runtime(second_request)

    assert first.identities == second.identities
    artifact = first_run / "artifacts" / "runtime-skills.json"
    assert artifact.is_file(), "workflow did not record trusted skill identities"
    persisted = json.loads(artifact.read_text(encoding="utf-8"))
    assert persisted["selected_skills"] == [
        identity.model_dump(mode="json") for identity in first.identities
    ]


def test_reference_only_skill_is_not_injected(tmp_path: Path) -> None:
    request, _run_dir = _request_for_category(tmp_path, "pwn")

    runtime = _runtime(request)

    assert "ctf-writeup" not in runtime.selected_ids
    assert all(identity.runtime_mode.value == "injected" for identity in runtime.identities)
    assert getattr(request, "developer", None) is not None
    agent_files = sorted((Path(__file__).parents[1] / ".codex" / "agents").glob("*.toml"))
    assert agent_files
    assert all(
        tomllib.loads(path.read_text(encoding="utf-8"))["runtime_mode"]
        == "reference_only"
        for path in agent_files
    )
