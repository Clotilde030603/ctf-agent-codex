from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from ctf_agent.models.base import ModelBackendError, ModelRequest, ModelResponse
from ctf_agent.models.claude import ClaudeStubBackend
from ctf_agent.models.codex import CodexCliBackend
from ctf_agent.scheduler import (
    ModelHypothesisPlanner,
    Scheduler,
    StaticHypothesisPlanner,
)
from ctf_agent.schemas import FlagCandidate, Hypothesis, SpecialistResult


class FakeSpecialist:
    def __init__(
        self,
        name: str,
        *,
        delay: float = 0.0,
        status: str = "inconclusive",
        categories: set[str] | None = None,
        flag: str | None = None,
    ) -> None:
        self.name = name
        self.delay = delay
        self.status = status
        self.categories = categories
        self.flag = flag

    def supports(self, category: str) -> bool:
        return self.categories is None or category in self.categories

    async def solve(self, hypothesis: Hypothesis, context: dict[str, object]) -> SpecialistResult:
        await asyncio.sleep(self.delay)
        return SpecialistResult(
            hypothesis_id=hypothesis.id,
            status=self.status,
            facts=[f"{self.name} handled {hypothesis.id}"],
            flag_candidates=[
                FlagCandidate(
                    value=self.flag,
                    source_artifact="solver-output",
                    source_location="stdout",
                    solver_command="python solve.py",
                )
            ]
            if self.flag
            else [],
        )


def test_model_planner_caps_hypotheses_at_three() -> None:
    backend = ClaudeStubBackend(
        [
            json.dumps(
                {
                    "hypotheses": [
                        {"id": "h1", "claim": "web"},
                        {"id": "h2", "claim": "rev"},
                        {"id": "h3", "claim": "crypto"},
                        {"id": "h4", "claim": "pwn"},
                    ]
                }
            )
        ]
    )

    hypotheses = asyncio.run(ModelHypothesisPlanner(backend).plan({"url": "https://ctf.local/1"}))

    assert [hypothesis.id for hypothesis in hypotheses] == ["h1", "h2", "h3"]
    assert backend.requests[0].context["url"] == "https://ctf.local/1"


def test_scheduler_runs_matching_specialists_concurrently() -> None:
    hypotheses = [
        Hypothesis(
            id="h1",
            claim="web lead",
            expected_signal="http behavior",
            cost="low",
            confidence=0.5,
            kill_condition="none",
            success_condition="flag",
        ),
        Hypothesis(
            id="h2",
            claim="crypto lead",
            expected_signal="decryption",
            cost="low",
            confidence=0.5,
            kill_condition="none",
            success_condition="flag",
        ),
    ]
    scheduler = Scheduler(
        planner=StaticHypothesisPlanner(hypotheses),
        specialists=(
            FakeSpecialist("web", delay=0.15, categories={"web lead"}),
            FakeSpecialist("crypto", delay=0.15, categories={"crypto lead"}),
        ),
    )

    started = time.perf_counter()
    result = asyncio.run(scheduler.run({}))
    elapsed = time.perf_counter() - started

    assert elapsed < 0.25
    assert result.stop_reason == "no_progress"
    assert {item.facts[0].split()[0] for item in result.specialist_results} == {"web", "crypto"}


def test_scheduler_stops_on_solved_and_preserves_flag() -> None:
    scheduler = Scheduler(
        planner=StaticHypothesisPlanner(
            [
                Hypothesis(
                    id="h1",
                    claim="lead",
                    expected_signal="flag",
                    cost="low",
                    confidence=0.5,
                    kill_condition="none",
                    success_condition="flag",
                )
            ]
        ),
        specialists=(FakeSpecialist("misc", status="confirmed", flag="flag{ok}"),),
    )

    result = asyncio.run(scheduler.run({}))

    assert result.solved is True
    assert result.stop_reason == "solved"
    assert result.accepted_flags == ("flag{ok}",)


def test_scheduler_no_progress_cutoff_after_first_round() -> None:
    scheduler = Scheduler(
        planner=StaticHypothesisPlanner(
            [
                Hypothesis(
                    id="h1",
                    claim="lead",
                    expected_signal="flag",
                    cost="low",
                    confidence=0.5,
                    kill_condition="none",
                    success_condition="flag",
                )
            ]
        ),
        specialists=(FakeSpecialist("misc"),),
        no_progress_cutoff=1,
        max_rounds=3,
    )

    result = asyncio.run(scheduler.run({}))

    assert result.stop_reason == "no_progress"
    assert len(result.specialist_results) == 1


def test_codex_cli_backend_validates_structured_json(tmp_path: Path) -> None:
    script = tmp_path / "codex_stub.py"
    script.write_text(
        "import json, sys\n"
        "payload = json.loads(sys.stdin.read())\n"
        "print(json.dumps({'content': 'ok', 'metadata': {'prompt': payload['prompt']}}))\n"
    )
    backend = CodexCliBackend(command=(sys.executable, str(script)))

    response = asyncio.run(backend.complete(ModelRequest(prompt="solve this")))

    assert response.content == "ok"
    assert response.metadata == {"prompt": "solve this"}


def test_codex_cli_backend_rejects_invalid_json(tmp_path: Path) -> None:
    script = tmp_path / "bad_codex_stub.py"
    script.write_text("print('not-json')\n")
    backend = CodexCliBackend(command=(sys.executable, str(script)))

    with pytest.raises(ModelBackendError):
        asyncio.run(backend.complete(ModelRequest(prompt="solve this")))


def test_claude_stub_records_requests_and_exhausts() -> None:
    backend = ClaudeStubBackend([ModelResponse(content="first")])

    response = asyncio.run(backend.complete(ModelRequest(prompt="hello")))

    assert response.content == "first"
    assert backend.requests[0].prompt == "hello"
    with pytest.raises(ModelBackendError):
        asyncio.run(backend.complete(ModelRequest(prompt="again")))
