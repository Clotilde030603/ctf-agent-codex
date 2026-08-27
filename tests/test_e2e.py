from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from ctf_agent.config import Settings
from ctf_agent.schemas import (
    Artifact,
    AuthSession,
    Challenge,
    FlagPolicy,
    RunState,
    SubmissionResult,
    SubmissionVerdict,
)
from ctf_agent.workflow import AutonomousWorkflow

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk/wcAAusB9Y9Z4ioAAAAASUVORK5CYII="
)


class FakeCTFdAdapter:
    async def authenticate(self) -> AuthSession:
        return AuthSession(authenticated=True)

    async def fetch_challenge(self, url: str) -> Challenge:
        return Challenge(
            id="7",
            url=url,
            event="fixture",
            title="Deterministic Warmup",
            description="Recover the flag from the supplied artifact.",
            category="forensics",
            points=100,
            flag_policy=FlagPolicy(pattern=r"flag\{[^{}]+\}", prefix="flag"),
            attachment_urls=["https://ctf.test/files/payload.txt"],
        )

    async def download_attachments(
        self, challenge: Challenge, destination: Path
    ) -> list[Artifact]:
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / "payload.txt"
        path.write_text("evidence: flag{fixture_vertical_slice}\n", encoding="utf-8")
        data = path.read_bytes()
        return [
            Artifact(
                path=path,
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
                source_url=challenge.attachment_urls[0],
                media_type="text/plain",
            )
        ]

    async def extract_flag_policy(self, challenge: Challenge) -> FlagPolicy:
        return challenge.flag_policy

    async def submit_flag(self, challenge: Challenge, flag: str) -> SubmissionResult:
        verdict = (
            SubmissionVerdict.ACCEPTED
            if flag == "flag{fixture_vertical_slice}"
            else SubmissionVerdict.WRONG
        )
        return SubmissionResult(verdict=verdict, message=verdict.value, status_code=200)

    async def capture_challenge(self, challenge: Challenge, destination: Path) -> Path:
        destination.write_bytes(PNG)
        return destination

    async def capture_verdict(self, challenge: Challenge, destination: Path) -> Path:
        destination.write_bytes(PNG)
        return destination


@pytest.mark.asyncio
async def test_fake_ctfd_end_to_end_vertical_slice(tmp_path: Path) -> None:
    settings = Settings(
        runs_dir=tmp_path / "runs",
        tool_timeout_seconds=10,
        submission_budget=2,
    )
    workflow = AutonomousWorkflow(settings, FakeCTFdAdapter())
    controller = workflow.controller()
    context = controller.create_run(
        "https://ctf.test/challenges/7", auto_submit=True, writeup=True
    )
    result = await controller.execute(context)

    assert result.state is RunState.DONE, result.last_error
    expected = {
        "challenge.json",
        "state.db",
        "triage.json",
        "hypotheses.json",
        "events.jsonl",
        "solve.py",
        "writeup.md",
    }
    assert expected.issubset({path.name for path in result.run_dir.iterdir()})
    assert (result.run_dir / "evidence" / "01-challenge.png").is_file()
    assert (result.run_dir / "evidence" / "02-exploit-proof.png").is_file()
    assert (result.run_dir / "evidence" / "03-accepted.png").is_file()
    manifest = json.loads((result.run_dir / "evidence" / "manifest.json").read_text())
    assert len(manifest["entries"]) == 4
    assert "flag{fixture_vertical_slice}" in (result.run_dir / "writeup.md").read_text()
    assert context.store.submission_count(result.run_id) == 1
