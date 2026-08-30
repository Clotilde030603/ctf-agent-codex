from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from ctf_agent.config import Settings
from ctf_agent.engine import RunContext
from ctf_agent.evidence import TerminalRenderResult
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
    VerifiedCandidateRecord,
)
from ctf_agent.workflow import AutonomousWorkflow
from ctf_agent.writeup.validator import FactValidationResult, WriteupValidator

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk/wcAAusB9Y9Z4ioAAAAASUVORK5CYII="
)


class FakeTerminalRenderer:
    def render(
        self,
        transcript: str | bytes,
        output_dir: Path,
        *,
        stem: str,
        command: str,
    ) -> TerminalRenderResult:
        html = output_dir / f"{stem}.html"
        png = output_dir / f"{stem}.png"
        html.write_text(str(transcript), encoding="utf-8")
        png.write_bytes(PNG)
        return TerminalRenderResult(html, png, False, "created")


class HtmlOnlyTerminalRenderer:
    def render(
        self,
        transcript: str | bytes,
        output_dir: Path,
        *,
        stem: str,
        command: str,
    ) -> TerminalRenderResult:
        html = output_dir / f"{stem}.html"
        html.write_text(str(transcript), encoding="utf-8")
        return TerminalRenderResult(html, None, False, "playwright-unavailable")


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


class FlakyEvidenceAdapter(FakeCTFdAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.fail_captures = True

    async def capture_challenge(self, challenge: Challenge, destination: Path) -> Path:
        if self.fail_captures:
            raise RuntimeError("challenge browser unavailable")
        return await super().capture_challenge(challenge, destination)

    async def capture_verdict(self, challenge: Challenge, destination: Path) -> Path:
        if self.fail_captures:
            raise RuntimeError("verdict browser unavailable")
        return await super().capture_verdict(challenge, destination)

def persist_verified_candidate(context: RunContext, candidate: FlagCandidate) -> None:
    source = context.record.run_dir / candidate.source_artifact
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(candidate.value + "\n", encoding="utf-8")
    solver = context.record.run_dir / "solve.py"
    solver.write_text(
        "from pathlib import Path\nprint(Path('files/payload.txt').read_text())\n",
        encoding="utf-8",
    )
    context.store.save_verified_candidate(
        VerifiedCandidateRecord(
            run_id=context.record.run_id,
            candidate=candidate,
            solver_sha256=hashlib.sha256(solver.read_bytes()).hexdigest(),
            source_artifact=candidate.source_artifact,
            source_artifact_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        )
    )


@pytest.mark.asyncio
async def test_fake_ctfd_end_to_end_vertical_slice(tmp_path: Path) -> None:
    settings = Settings(
        backend="static",
        runs_dir=tmp_path / "runs",
        tool_timeout_seconds=10,
        submission_budget=2,
        allow_local_reproduction=True,
        approve_static_submission=True,
    )
    workflow = AutonomousWorkflow(
        settings,
        FakeCTFdAdapter(),
        terminal_renderer=FakeTerminalRenderer(),
    )
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
        "writeup.html",
        "provenance.json",
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
        approve_static_submission=True,
    )
    adapter = FakeCTFdAdapter()
    workflow = AutonomousWorkflow(
        settings, adapter, terminal_renderer=FakeTerminalRenderer()
    )
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
        approve_static_submission=True,
    )
    adapter = FakeCTFdAdapter()
    workflow = AutonomousWorkflow(
        settings, adapter, terminal_renderer=FakeTerminalRenderer()
    )
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
        terminal_renderer=FakeTerminalRenderer(),
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
    assert candidate["data_dependency_verified"] is True
    assert candidate["independent_verified"] is False
    assert candidate["submission_allowed"] is False


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
        data_dependency_verified=True,
        independent_verified=True,
        submission_allowed=True,
    )
    persist_verified_candidate(context, context.values["candidate"])

    outcome = await workflow.submit(context)

    assert outcome.target is RunState.AUTHENTICATE
    assert context.store.submission_count(context.record.run_id) == 0
    assert context.store.pending_submission(context.record.run_id) is None


@pytest.mark.asyncio
async def test_evidence_records_terminal_capture_failure_without_fake_png(
    tmp_path: Path,
) -> None:
    adapter = FakeCTFdAdapter()
    workflow = AutonomousWorkflow(
        Settings(backend="static", runs_dir=tmp_path / "runs"),
        adapter,
        terminal_renderer=HtmlOnlyTerminalRenderer(),
    )
    context = workflow.controller().create_run(
        "https://ctf.test/challenges/12", auto_submit=True, writeup=True
    )
    context.values["challenge"] = await adapter.fetch_challenge(
        "https://ctf.test/challenges/12"
    )
    context.values["candidate"] = FlagCandidate(
        value="flag{evidence_failure}",
        source_artifact="files/payload.txt",
        source_location="line 1",
        derivation=["fixture"],
        solver_command="python3 solve.py",
        format_match=True,
        provenance_verified=True,
        replay_verified=True,
        data_dependency_verified=True,
        independent_verified=True,
        submission_allowed=True,
    )
    persist_verified_candidate(context, context.values["candidate"])

    outcome = await workflow.evidence(context)

    evidence_dir = context.record.run_dir / "evidence"
    assert outcome.target is RunState.WRITEUP_PENDING
    assert not (evidence_dir / "02-exploit-proof.png").exists()
    manifest = json.loads((evidence_dir / "manifest.json").read_text())
    assert any(event["stage"] == "EVIDENCE_FAILURE" for event in manifest["events"])


@pytest.mark.asyncio
async def test_accepted_evidence_failure_is_retryable_without_resubmit(
    tmp_path: Path,
) -> None:
    adapter = FlakyEvidenceAdapter()
    settings = Settings(
        backend="static",
        runs_dir=tmp_path / "runs",
        allow_local_reproduction=True,
        approve_static_submission=True,
    )
    workflow = AutonomousWorkflow(
        settings,
        adapter,
        terminal_renderer=FakeTerminalRenderer(),
    )
    controller = workflow.controller()
    context = controller.create_run(
        "https://ctf.test/challenges/retry-evidence",
        auto_submit=True,
        writeup=True,
    )

    first = await controller.execute(context)

    assert first.state is RunState.DONE_WITH_WARNINGS
    assert len(adapter.submitted) == 1
    manifest = json.loads((first.run_dir / "evidence" / "manifest.json").read_text())
    assert manifest["failures"]
    assert (first.run_dir / "evidence" / "03-verdict-fallback.json").is_file()

    adapter.fail_captures = False
    retry_context = controller.retry_evidence(first.run_id)
    retried = await controller.execute(retry_context)

    assert retried.state is RunState.DONE
    assert len(adapter.submitted) == 1
    retried_manifest = json.loads(
        (retried.run_dir / "evidence" / "manifest.json").read_text()
    )
    assert retried_manifest["failures"] == []


@pytest.mark.asyncio
async def test_writeup_failure_stays_recoverable_after_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = FakeCTFdAdapter()
    workflow = AutonomousWorkflow(
        Settings(
            backend="static",
            runs_dir=tmp_path / "runs",
            allow_local_reproduction=True,
            approve_static_submission=True,
        ),
        adapter,
        terminal_renderer=FakeTerminalRenderer(),
    )
    controller = workflow.controller()
    context = controller.create_run(
        "https://ctf.test/challenges/writeup-retry",
        auto_submit=True,
        writeup=True,
    )
    original = WriteupValidator.validate_all
    monkeypatch.setattr(
        WriteupValidator,
        "validate_all",
        lambda self, run_dir: FactValidationResult(False, ("forced failure",)),
    )

    first = await controller.execute(context)

    assert first.state is RunState.WRITEUP_PENDING
    assert first.last_error == "write-up validation failed: forced failure"
    assert len(adapter.submitted) == 1

    monkeypatch.setattr(WriteupValidator, "validate_all", original)
    resumed = controller.resume_run(first.run_id)
    finished = await controller.execute(resumed)

    assert finished.state is RunState.DONE
    assert len(adapter.submitted) == 1


def test_verified_record_is_invalidated_when_solver_changes(tmp_path: Path) -> None:
    workflow = AutonomousWorkflow(
        Settings(backend="static", runs_dir=tmp_path / "runs"),
        FakeCTFdAdapter(),
    )
    context = workflow.controller().create_run(
        "https://ctf.test/challenges/tamper",
        auto_submit=False,
        writeup=False,
    )
    candidate = FlagCandidate(
        value="flag{tamper}",
        source_artifact="files/payload.txt",
        source_location="line 1",
        solver_command="python3 solve.py",
        format_match=True,
        provenance_verified=True,
        replay_verified=True,
        data_dependency_verified=True,
    )
    persist_verified_candidate(context, candidate)
    (context.record.run_dir / "solve.py").write_text("print('changed')\n")

    with pytest.raises(RuntimeError, match="solver SHA-256 changed"):
        workflow._candidate(context)
    record = context.store.load_verified_candidate(context.record.run_id)
    assert record is not None
    assert record.valid is False


@pytest.mark.asyncio
async def test_submission_recomputes_gate_from_individual_verification_fields(
    tmp_path: Path,
) -> None:
    workflow = AutonomousWorkflow(
        Settings(backend="static", runs_dir=tmp_path / "runs"),
        FakeCTFdAdapter(),
    )
    context = workflow.controller().create_run(
        "https://ctf.test/challenges/gate",
        auto_submit=True,
        writeup=False,
    )
    context.values["challenge"] = await FakeCTFdAdapter().fetch_challenge(
        context.record.challenge_url
    )
    candidate = FlagCandidate(
        value="flag{gate}",
        source_artifact="files/payload.txt",
        source_location="line 1",
        solver_command="python3 solve.py",
        format_match=True,
        provenance_verified=True,
        replay_verified=True,
        data_dependency_verified=False,
        independent_verified=True,
        submission_allowed=True,
    )
    persist_verified_candidate(context, candidate)

    with pytest.raises(RuntimeError, match="data_dependency_verified"):
        await workflow.submit(context)
