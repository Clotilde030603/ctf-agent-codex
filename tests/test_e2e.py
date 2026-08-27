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
    FlagCandidate,
    FlagPolicy,
    RunState,
    SpecialistResult,
    SubmissionResult,
    SubmissionVerdict,
)
from ctf_agent.workflow import AutonomousWorkflow

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk/wcAAusB9Y9Z4ioAAAAASUVORK5CYII="
)


class FakeCTFdAdapter:
    def __init__(self) -> None:
        self.submitted: list[str] = []

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
        self.submitted.append(flag)
        verdict = (
            SubmissionVerdict.ACCEPTED
            if flag == "flag{fixture_vertical_slice}"
            else SubmissionVerdict.WRONG
        )
        return SubmissionResult(verdict=verdict, message=verdict.value, status_code=200)

    async def resolve_submission(
        self, challenge: Challenge, flag: str
    ) -> SubmissionResult | None:
        if flag == "flag{fixture_vertical_slice}":
            return SubmissionResult(
                verdict=SubmissionVerdict.ALREADY_SOLVED,
                message="resolved from platform state",
                status_code=200,
            )
        return None

    async def capture_challenge(self, challenge: Challenge, destination: Path) -> Path:
        destination.write_bytes(PNG)
        return destination

    async def capture_verdict(self, challenge: Challenge, destination: Path) -> Path:
        destination.write_bytes(PNG)
        return destination


@pytest.mark.asyncio
async def test_fake_ctfd_end_to_end_vertical_slice(tmp_path: Path) -> None:
    settings = Settings(
        backend="static",
        runs_dir=tmp_path / "runs",
        tool_timeout_seconds=10,
        submission_budget=2,
        allow_local_reproduction=True,
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


@pytest.mark.asyncio
async def test_pending_submission_is_resolved_without_duplicate_submit(tmp_path: Path) -> None:
    settings = Settings(
        backend="static",
        runs_dir=tmp_path / "runs",
        tool_timeout_seconds=10,
        submission_budget=2,
        allow_local_reproduction=True,
    )
    adapter = FakeCTFdAdapter()
    workflow = AutonomousWorkflow(settings, adapter)
    controller = workflow.controller()
    context = controller.create_run(
        "https://ctf.test/challenges/7", auto_submit=True, writeup=True
    )
    context.store.begin_submission(
        context.record.run_id, "flag{fixture_vertical_slice}", "crash-window-attempt"
    )

    result = await controller.execute(context)

    assert result.state is RunState.DONE, result.last_error
    assert adapter.submitted == []
    assert context.store.submission_count(result.run_id) == 1


@pytest.mark.asyncio
async def test_completed_submission_is_not_repeated_after_resume_window(tmp_path: Path) -> None:
    settings = Settings(
        backend="static",
        runs_dir=tmp_path / "runs",
        tool_timeout_seconds=10,
        submission_budget=2,
        allow_local_reproduction=True,
    )
    adapter = FakeCTFdAdapter()
    workflow = AutonomousWorkflow(settings, adapter)
    controller = workflow.controller()
    context = controller.create_run(
        "https://ctf.test/challenges/7", auto_submit=True, writeup=True
    )
    context.store.record_submission(
        context.record.run_id,
        "flag{fixture_vertical_slice}",
        SubmissionVerdict.ACCEPTED.value,
    )

    result = await controller.execute(context)

    assert result.state is RunState.DONE, result.last_error
    assert adapter.submitted == []


@pytest.mark.asyncio
async def test_verification_rejection_returns_to_plan(tmp_path: Path) -> None:
    workflow = AutonomousWorkflow(
        Settings(
            backend="static",
            runs_dir=tmp_path / "runs",
            allow_local_reproduction=True,
        ),
        FakeCTFdAdapter(),
    )
    controller = workflow.controller()
    context = controller.create_run(
        "https://ctf.test/challenges/7", auto_submit=True, writeup=True
    )
    context.values["challenge"] = await FakeCTFdAdapter().fetch_challenge(
        context.record.challenge_url
    )
    context.values["specialist_results"] = [
        SpecialistResult(
            hypothesis_id="H1",
            status="confirmed",
            flag_candidates=[
                FlagCandidate(
                    value="flag{sample}",
                    source_artifact="files/payload.txt",
                    source_location="offset 0",
                    derivation=["fixture"],
                    solver_command="python3 solve.py",
                )
            ],
        )
    ]

    outcome = await workflow.verify(context)

    assert outcome.target is RunState.PLAN
    assert outcome.payload["accepted"] is False


@pytest.mark.asyncio
async def test_verified_dry_run_stops_cleanly_in_ready_state(tmp_path: Path) -> None:
    adapter = FakeCTFdAdapter()
    workflow = AutonomousWorkflow(
        Settings(
            backend="static",
            runs_dir=tmp_path / "runs",
            allow_local_reproduction=True,
        ),
        adapter,
    )
    context = workflow.controller().create_run(
        "https://ctf.test/challenges/7", auto_submit=False, writeup=False
    )

    result = await workflow.controller().execute(context)

    assert result.state is RunState.READY, result.last_error
    assert adapter.submitted == []
    artifact = json.loads((result.run_dir / "verified-candidate.json").read_text())
    candidate = artifact["candidate"]
    assert candidate["format_match"] is True
    assert candidate["provenance_verified"] is True
    assert candidate["replay_verified"] is True
    assert candidate["independent_verified"] is True
    assert candidate["submission_allowed"] is True


@pytest.mark.asyncio
async def test_blind_gate_rejects_hardcoded_solver_in_workflow(tmp_path: Path) -> None:
    workflow = AutonomousWorkflow(
        Settings(backend="static", runs_dir=tmp_path / "runs"),
        FakeCTFdAdapter(),
    )
    context = workflow.controller().create_run(
        "https://ctf.test/challenges/10", auto_submit=True, writeup=False
    )
    source = context.record.run_dir / "files" / "payload.txt"
    source.write_text("flag{hardcoded_workflow}\n", encoding="utf-8")
    (context.record.run_dir / "solve.py").write_text(
        "print('flag{hardcoded_workflow}')\n", encoding="utf-8"
    )
    context.values["challenge"] = Challenge(
        id="10",
        url="https://ctf.test/challenges/10",
        title="Hardcoded",
        flag_policy=FlagPolicy(pattern=r"flag\{[^{}]+\}"),
    )
    context.values["specialist_results"] = [
        SpecialistResult(
            hypothesis_id="H1",
            status="confirmed",
            flag_candidates=[
                FlagCandidate(
                    value="flag{hardcoded_workflow}",
                    source_artifact="files/payload.txt",
                    source_location="line 1",
                    derivation=["fixture"],
                    solver_command="python3 solve.py",
                )
            ],
        )
    ]

    outcome = await workflow.verify(context)

    assert outcome.target is RunState.PLAN
    assert any("hardcode" in reason for reason in outcome.payload["reasons"])


@pytest.mark.asyncio
async def test_auth_required_submission_returns_to_auth_without_budget_cost(
    tmp_path: Path,
) -> None:
    class ExpiredAdapter(FakeCTFdAdapter):
        async def submit_flag(
            self, challenge: Challenge, flag: str
        ) -> SubmissionResult:
            return SubmissionResult(
                verdict=SubmissionVerdict.AUTH_REQUIRED,
                message="session expired",
                status_code=401,
            )

    adapter = ExpiredAdapter()
    workflow = AutonomousWorkflow(
        Settings(backend="static", runs_dir=tmp_path / "runs"), adapter
    )
    context = workflow.controller().create_run(
        "https://ctf.test/challenges/11", auto_submit=True, writeup=False
    )
    context.values["challenge"] = await adapter.fetch_challenge(
        "https://ctf.test/challenges/11"
    )
    context.values["candidate"] = FlagCandidate(
        value="flag{verified}",
        source_artifact="files/payload.txt",
        source_location="line 1",
        derivation=["fixture"],
        solver_command="python3 solve.py",
        format_match=True,
        provenance_verified=True,
        replay_verified=True,
        independent_verified=True,
        submission_allowed=True,
    )

    outcome = await workflow.submit(context)

    assert outcome.target is RunState.AUTHENTICATE
    assert context.store.submission_count(context.record.run_id) == 0
    assert context.store.pending_submission(context.record.run_id) is None
