from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from ctf_agent.config import Settings
from ctf_agent.ingestion.session import ScopedAsyncSession
from ctf_agent.platforms.ctfd import CTFdPlatformAdapter
from ctf_agent.schemas import RunState
from ctf_agent.scope import HostScope
from ctf_agent.workflow import AutonomousWorkflow

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "/wcAAusB9Y9Z4ioAAAAASUVORK5CYII="
)


class EvidenceCTFdAdapter(CTFdPlatformAdapter):
    async def capture_challenge(self, challenge: object, destination: Path) -> Path:
        destination.write_bytes(PNG)
        return destination

    async def capture_verdict(self, challenge: object, destination: Path) -> Path:
        destination.write_bytes(PNG)
        return destination


def fixture_adapter(submitted: list[str]) -> tuple[httpx.AsyncClient, EvidenceCTFdAdapter]:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/users/me":
            return httpx.Response(200, json={"success": True, "data": {"id": 1}})
        if request.url.path == "/api/v1/challenges/7":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "id": 7,
                        "name": "Mock CTFd Warmup",
                        "description": "Inspect the attachment.",
                        "category": "forensics",
                        "value": 50,
                        "files": ["/files/payload.txt"],
                        "flag_pattern": r"flag\{[^{}]+\}",
                        "flag_prefix": "flag",
                    },
                },
            )
        if request.url.path == "/files/payload.txt":
            return httpx.Response(
                200,
                content=b"fixture flag{mock_ctfd_accepted}\n",
                headers={"content-type": "text/plain"},
            )
        if request.url.path == "/api/v1/challenges/attempt":
            payload = json.loads(request.content)
            submitted.append(payload["submission"])
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"status": "correct", "message": "Solved"},
                },
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    scope = HostScope.from_url("https://ctf.test/challenges/7")
    session = ScopedAsyncSession(scope, client=client)
    return client, EvidenceCTFdAdapter("https://ctf.test", session=session)


@pytest.mark.asyncio
async def test_mock_ctfd_api_full_workflow(tmp_path: Path) -> None:
    submitted: list[str] = []
    client, adapter = fixture_adapter(submitted)
    workflow = AutonomousWorkflow(
        Settings(runs_dir=tmp_path / "runs", tool_timeout_seconds=5), adapter
    )
    controller = workflow.controller()
    context = controller.create_run(
        "https://ctf.test/challenges/7", auto_submit=True, writeup=True
    )
    result = await controller.execute(context)
    await client.aclose()

    assert result.state is RunState.DONE, result.last_error
    assert submitted == ["flag{mock_ctfd_accepted}"]
    assert (result.run_dir / "requirements.txt").is_file()
    assert (result.run_dir / "writeup.md").is_file()


@pytest.mark.asyncio
async def test_resume_from_ingest_checkpoint(tmp_path: Path) -> None:
    submitted: list[str] = []
    client, adapter = fixture_adapter(submitted)
    settings = Settings(runs_dir=tmp_path / "runs", tool_timeout_seconds=5)
    workflow = AutonomousWorkflow(settings, adapter)
    controller = workflow.controller()
    context = controller.create_run(
        "https://ctf.test/challenges/7", auto_submit=True, writeup=True
    )
    context.record = context.store.transition(context.record.run_id, RunState.INGEST)

    resumed = controller.resume_run(context.record.run_id)
    result = await controller.execute(resumed)
    await client.aclose()

    assert result.state is RunState.DONE, result.last_error
    assert submitted == ["flag{mock_ctfd_accepted}"]
    assert any(
        event["event_type"] == "run.resumed"
        for event in resumed.ledger.list(result.run_id)
    )
