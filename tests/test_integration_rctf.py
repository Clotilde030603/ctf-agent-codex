from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from ctf_agent.config import Settings
from ctf_agent.evidence import TerminalRenderResult
from ctf_agent.ingestion.session import ScopedAsyncSession
from ctf_agent.platforms.base import SubmissionVerdict
from ctf_agent.platforms.rctf import RCTFPlatformAdapter, parse_rctf_submission
from ctf_agent.schemas import RunState
from ctf_agent.scope import HostScope
from ctf_agent.workflow import AutonomousWorkflow

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "/wcAAusB9Y9Z4ioAAAAASUVORK5CYII="
)


class EvidenceRCTFAdapter(RCTFPlatformAdapter):
    async def capture_challenge(self, challenge: object, destination: Path) -> Path:
        destination.write_bytes(PNG)
        return destination

    async def capture_verdict(self, challenge: object, destination: Path) -> Path:
        destination.write_bytes(PNG)
        return destination


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

def rctf_fixture(
    submitted: list[str], solved: bool = False
) -> tuple[ScopedAsyncSession, RCTFPlatformAdapter]:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/test":
            return httpx.Response(200, json={"kind": "goodToken"})
        if request.url.path == "/api/v2/challs":
            return httpx.Response(404, json={"kind": "badRoute"})
        if request.url.path == "/api/v1/challs":
            return httpx.Response(
                200,
                json={
                    "kind": "goodChallenges",
                    "data": [
                        {
                            "id": "web-warmup",
                            "name": "Web Warmup",
                            "description": "Inspect the source.",
                            "category": "web",
                            "points": 100,
                            "hasFlag": True,
                            "solvesByUser": solved,
                            "files": ["source.zip"],
                        }
                    ],
                },
            )
        if request.url.path == "/api/v1/challs/web-warmup/files/source.zip":
            return httpx.Response(200, content=b"flag{rctf_fixture}\n")
        if request.url.path == "/api/v1/challs/web-warmup/submit":
            submitted.append(json.loads(request.content)["flag"])
            return httpx.Response(200, json={"kind": "goodFlag", "data": {}})
        return httpx.Response(404, json={"kind": "badRoute"})

    session = ScopedAsyncSession(
        HostScope.from_url("https://rctf.test", allow_private_hosts=True),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    return session, EvidenceRCTFAdapter("https://rctf.test", session=session)


@pytest.mark.asyncio
async def test_rctf_fetch_download_submit_and_resolve(tmp_path: Path) -> None:
    submitted: list[str] = []
    session, adapter = rctf_fixture(submitted, solved=True)

    auth = await adapter.authenticate()
    challenge = await adapter.fetch_challenge("https://rctf.test/challs/web-warmup")
    artifacts = await adapter.download_attachments(challenge, tmp_path)
    result = await adapter.submit_flag(challenge, "flag{rctf_fixture}")
    resolved = await adapter.resolve_submission(challenge, "flag{rctf_fixture}")
    await session.aclose()

    assert auth.authenticated
    assert challenge.id == "web-warmup"
    assert challenge.title == "Web Warmup"
    assert challenge.category == "web"
    assert challenge.points == 100
    assert challenge.metadata["hasFlag"] is True
    assert artifacts[0].path.read_bytes() == b"flag{rctf_fixture}\n"
    assert submitted == ["flag{rctf_fixture}"]
    assert result.verdict is SubmissionVerdict.ACCEPTED
    assert resolved is not None
    assert resolved.verdict is SubmissionVerdict.ALREADY_SOLVED


def test_rctf_verdict_parser_maps_official_kinds() -> None:
    assert parse_rctf_submission({"kind": "goodFlag"}).verdict is SubmissionVerdict.ACCEPTED
    assert parse_rctf_submission({"kind": "badFlag"}).verdict is SubmissionVerdict.WRONG
    assert (
        parse_rctf_submission({"kind": "badAlreadySolvedChallenge"}).verdict
        is SubmissionVerdict.ALREADY_SOLVED
    )
    rate = parse_rctf_submission(
        {"kind": "badRateLimit", "data": {"timeLeft": 1234}}, status_code=429
    )
    assert rate.verdict is SubmissionVerdict.RATE_LIMITED
    assert "1234" in rate.message
    assert (
        parse_rctf_submission({"kind": "badAuth"}, status_code=401).verdict
        is SubmissionVerdict.AUTH_REQUIRED
    )


@pytest.mark.asyncio
async def test_public_rctf_challenge_list_is_not_authentication() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/test":
            return httpx.Response(404, json={"kind": "badRoute"})
        if request.url.path == "/api/v2/challs":
            return httpx.Response(200, json={"kind": "goodChallengesV2", "data": []})
        return httpx.Response(404, json={})

    session = ScopedAsyncSession(
        HostScope.from_url("https://rctf.test", allow_private_hosts=True),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    adapter = RCTFPlatformAdapter("https://rctf.test", session=session)

    auth = await adapter.authenticate()
    await session.aclose()

    assert auth.authenticated is False


@pytest.mark.asyncio
async def test_fake_rctf_full_workflow_reaches_done(tmp_path: Path) -> None:
    submitted: list[str] = []
    session, adapter = rctf_fixture(submitted, solved=False)
    workflow = AutonomousWorkflow(
        Settings(
            backend="static",
            approve_static_submission=True,
            runs_dir=tmp_path / "runs",
            allow_local_reproduction=True,
        ),
        adapter,
        terminal_renderer=FakeTerminalRenderer(),
    )
    context = workflow.controller().create_run(
        "https://rctf.test/challs/web-warmup",
        auto_submit=True,
        writeup=True,
    )

    result = await workflow.controller().execute(context)
    await session.aclose()

    assert result.state is RunState.DONE, result.last_error
    assert submitted == ["flag{rctf_fixture}"]
    assert (result.run_dir / "evidence" / "03-accepted.png").is_file()
    assert (result.run_dir / "writeup.md").is_file()
    assert (result.run_dir / "writeup.html").is_file()
    assert (result.run_dir / "provenance.json").is_file()
