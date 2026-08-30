from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from ctf_agent.ingestion.session import ScopedAsyncSession, SessionConfig
from ctf_agent.models.base import ModelRequest, ModelResponse
from ctf_agent.scope import HostScope
from ctf_agent.workers import CommandPolicy, LaneWorkspace, WorkerBudget, WorkerCore, WorkerDecision


class QueueBackend:
    def __init__(self, decisions: list[dict[str, Any]]) -> None:
        self.decisions = list(decisions)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self.decisions:
            return ModelResponse(content=json.dumps({"action": "finish", "message": "done"}))
        return ModelResponse(content=json.dumps(self.decisions.pop(0)))


def test_worker_decision_schema_rejects_shell_strings() -> None:
    with pytest.raises(ValidationError):
        WorkerDecision.model_validate({"action": "run", "argv": ["bash", "-lc", "echo no"]})
    with pytest.raises(ValidationError):
        WorkerDecision.model_validate(
            {"action": "write_file", "path": "../x", "content": "x", "argv": ["python3"]}
        )
    assert WorkerDecision.model_validate({"action": "finish", "message": "ok"}).action == "finish"
    with pytest.raises(ValidationError):
        WorkerDecision.model_validate(
            {
                "action": "http_request",
                "method": "GET",
                "url": "https://challenge.test/",
                "headers": {"Authorization": "secret"},
            }
        )


def test_worker_http_action_is_host_scoped_and_sanitized(tmp_path: Path) -> None:
    async def run() -> tuple[Any, list[httpx.Request]]:
        requests: list[httpx.Request] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, text="token=supersecret\nresult=ok", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        session = ScopedAsyncSession(
            HostScope.from_url("https://challenge.test"),
            config=SessionConfig(rate_limit_per_second=1000),
            client=client,
        )
        backend = QueueBackend(
            [
                {
                    "action": "http_request",
                    "method": "GET",
                    "url": "https://challenge.test/api/data",
                },
                {
                    "action": "http_request",
                    "method": "GET",
                    "url": "https://outside.test/escape",
                },
                {"action": "finish", "message": "done"},
            ]
        )
        worker = WorkerCore(
            backend,
            LaneWorkspace(tmp_path / "lane"),
            budget=WorkerBudget(max_steps=4, max_http_requests=2),
            http_session=session,
        )
        try:
            return await worker.run("probe authorized service"), requests
        finally:
            await client.aclose()

    result, requests = asyncio.run(run())

    assert result.status == "finished"
    assert result.http_requests_run == 1
    assert len(requests) == 1
    first, second = result.reports[:2]
    assert first.status_code == 200
    assert first.response_artifact is not None
    response_text = Path(first.response_artifact).read_text()
    assert "token=[REDACTED]" in response_text
    assert "supersecret" not in response_text
    assert second.status == "failed"
    assert "outside allowed scope" in second.message


def test_lane_workspace_enforces_relative_paths(tmp_path: Path) -> None:
    workspace = LaneWorkspace(tmp_path / "lane")
    written = workspace.write_relative_file("notes/result.txt", "ok")
    assert written.read_text() == "ok"
    with pytest.raises(RuntimeError):
        workspace.resolve_relative("../escape.txt")
    with pytest.raises(RuntimeError):
        workspace.resolve_relative(str(tmp_path / "absolute.txt"))


def test_default_docker_command_wrapper_mounts_challenge_readonly(tmp_path: Path) -> None:
    challenge = tmp_path / "challenge"
    challenge.mkdir()
    workspace = LaneWorkspace(tmp_path / "lane", challenge_files=challenge)
    worker = WorkerCore(QueueBackend([]), workspace)

    command = worker._execution_command(["python3", "solve.py"])

    assert command[:4] == ["docker", "run", "--rm", "--network=none"]
    assert "--read-only" in command
    assert "--tmpfs=/tmp:rw,noexec,nosuid,size=64m" in command
    assert f"--mount=type=bind,src={workspace.root},dst=/work" in command
    assert f"--mount=type=bind,src={challenge.resolve()},dst=/challenge,readonly" in command
    assert command[-2:] == ["python3", "solve.py"]


def test_local_worker_runs_commands_writes_sanitized_artifacts_and_skips_duplicates(
    tmp_path: Path,
) -> None:
    workspace = LaneWorkspace(tmp_path / "lane")
    backend = QueueBackend(
        [
            {"action": "write_file", "path": "solve.py", "content": "token=supersecret\n"},
            {
                "action": "run",
                "argv": [
                    sys.executable,
                    "-c",
                    "print('token=supersecret'); import sys; print('ok', file=sys.stderr)",
                ],
            },
            {
                "action": "run",
                "argv": [
                    sys.executable,
                    "-c",
                    "print('token=supersecret'); import sys; print('ok', file=sys.stderr)",
                ],
            },
            {"action": "finish", "message": "complete"},
        ]
    )
    worker = WorkerCore(
        backend,
        workspace,
        budget=WorkerBudget(max_steps=5, max_commands=3, command_timeout_seconds=5),
        policy=CommandPolicy(allowed_argv0={Path(sys.executable).name}, local_test_mode=True),
    )

    result = asyncio.run(worker.run("solve"))

    assert result.status == "finished"
    assert result.commands_run == 1
    write_report = result.reports[0]
    assert write_report.action == "write_file"
    assert write_report.redacted is True
    assert "token=[REDACTED]" in (workspace.root / "solve.py").read_text()
    run_report = result.reports[1]
    assert run_report.exit_code == 0
    assert run_report.stdout_artifact is not None
    assert "token=[REDACTED]" in Path(run_report.stdout_artifact).read_text()
    assert "supersecret" not in Path(run_report.stdout_artifact).read_text()
    assert result.reports[2].status == "skipped"
    assert backend.requests[0].output_schema is not None


def test_worker_blocks_disallowed_argv_and_exhausts_no_progress(tmp_path: Path) -> None:
    workspace = LaneWorkspace(tmp_path / "lane")
    backend = QueueBackend(
        [
            {"action": "run", "argv": ["curl", "https://example.test"]},
            {"action": "run", "argv": ["curl", "https://example.test"]},
        ]
    )
    worker = WorkerCore(
        backend,
        workspace,
        budget=WorkerBudget(max_steps=5, max_commands=2, max_no_progress_steps=2),
        policy=CommandPolicy(allowed_argv0={"python3"}, local_test_mode=True),
    )

    result = asyncio.run(worker.run("try network"))

    assert result.status == "budget_exhausted"
    assert result.message == "no progress budget exhausted"
    assert [report.status for report in result.reports] == ["failed", "failed"]


def test_worker_times_out_and_preserves_stderr_stdout_artifacts(tmp_path: Path) -> None:
    workspace = LaneWorkspace(tmp_path / "lane")
    backend = QueueBackend(
        [
            {
                "action": "run",
                "argv": [sys.executable, "-c", "import time; print('start'); time.sleep(2)"],
            },
            {"action": "finish", "message": "done"},
        ]
    )
    worker = WorkerCore(
        backend,
        workspace,
        budget=WorkerBudget(max_steps=2, max_commands=1, command_timeout_seconds=0.1),
        policy=CommandPolicy(allowed_argv0={Path(sys.executable).name}, local_test_mode=True),
    )

    result = asyncio.run(worker.run("timeout"))

    report = result.reports[0]
    assert report.status == "timeout"
    assert report.exit_code == 124
    assert report.stdout_artifact is not None
    assert report.stderr_artifact is not None
    assert report.metadata_artifact is not None
    metadata = json.loads(Path(report.metadata_artifact).read_text())
    assert metadata["timed_out"] is True


def test_worker_aggregates_facts_candidates_and_written_files(tmp_path: Path) -> None:
    workspace = LaneWorkspace(tmp_path / "lane")
    candidate = {
        "value": "flag{worker_core}",
        "source_artifact": "stdout",
        "source_location": "line 1",
        "solver_command": "python3 solve.py",
    }
    backend = QueueBackend(
        [
            {
                "action": "write_file",
                "path": "solve.py",
                "content": "print('flag{worker_core}')\n",
                "facts": ["solver writes candidate"],
                "flag_candidates": [candidate],
            },
            {
                "action": "finish",
                "message": "done",
                "facts": ["solver writes candidate", "finished with replayable candidate"],
                "flag_candidates": [candidate],
            },
        ]
    )
    worker = WorkerCore(
        backend,
        workspace,
        budget=WorkerBudget(max_steps=3),
        policy=CommandPolicy(local_test_mode=True),
    )

    result = asyncio.run(worker.run("aggregate"))

    assert result.status == "finished"
    assert result.facts == ["solver writes candidate", "finished with replayable candidate"]
    assert [item.value for item in result.flag_candidates] == ["flag{worker_core}"]
    assert result.written_files == [str(workspace.root / "solve.py")]
    assert result.reports[0].made_progress is True
    assert result.reports[1].made_progress is True


def test_identical_writes_do_not_reset_no_progress(tmp_path: Path) -> None:
    workspace = LaneWorkspace(tmp_path / "lane")
    backend = QueueBackend(
        [
            {"action": "write_file", "path": "notes.txt", "content": "same\n"},
            {"action": "write_file", "path": "notes.txt", "content": "same\n"},
            {"action": "write_file", "path": "notes.txt", "content": "same\n"},
            {"action": "finish", "message": "finish text is not progress"},
        ]
    )
    worker = WorkerCore(
        backend,
        workspace,
        budget=WorkerBudget(max_steps=5, max_no_progress_steps=2),
        policy=CommandPolicy(local_test_mode=True),
    )

    result = asyncio.run(worker.run("identical writes"))

    assert result.status == "budget_exhausted"
    assert result.message == "no progress budget exhausted"
    assert [report.made_progress for report in result.reports] == [True, False, False]
    assert result.written_files == [str(workspace.root / "notes.txt")]
