from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ctf_agent.config import Settings
from ctf_agent.models.base import ModelBackendError, ModelResponse
from ctf_agent.models.claude import ClaudeStubBackend
from ctf_agent.schemas import Challenge, FlagPolicy, RunState
from ctf_agent.workflow import AutonomousWorkflow


def _planner_response() -> str:
    return json.dumps(
        {
            "hypotheses": [
                {
                    "id": "H-model",
                    "claim": "decode the XOR-protected payload",
                    "supporting_evidence": ["crypto constant at payload.bin:0"],
                    "expected_signal": "decoded plaintext with a flag",
                    "cost": "medium",
                    "confidence": 0.72,
                    "required_tools": ["python"],
                    "kill_condition": "decoded bytes fail the flag policy",
                    "success_condition": "solver reproduces a provenance-backed flag",
                }
            ]
        }
    )


def _context(tmp_path: Path, backend: ClaudeStubBackend, **settings: object):
    workflow = AutonomousWorkflow(
        Settings(
            runs_dir=tmp_path / "runs",
            backend="codex",
            **settings,
        ),
        planner_backend=backend,
    )
    controller = workflow.controller()
    context = controller.create_run(
        "https://ctf.test/challenges/7", auto_submit=False, writeup=False
    )
    context.values["challenge"] = Challenge(
        id="7",
        url="https://ctf.test/challenges/7",
        title="Model Planner Fixture",
        description="Recover the protected flag.",
        category="crypto-binary",
        flag_policy=FlagPolicy(pattern=r"flag\{[^{}]+\}"),
        service_hosts=["challenge.ctf.test"],
    )
    triage = {
        "classification": {
            "primary_category": "crypto-binary",
            "secondary_categories": [],
            "confidence": 0.9,
            "evidence": [{"reason": "XOR constant detected"}],
        },
        "files": [
            {
                "relative_path": "payload.bin",
                "size": 16,
                "sha256": "a" * 64,
                "mime": "application/octet-stream",
                "magic": "data",
                "entropy": 7.1,
                "language": None,
                "indicators": [
                    {
                        "kind": "crypto-constant",
                        "value": "0x2a",
                        "artifact_path": "payload.bin",
                        "offset": 0,
                    }
                ],
                "tool_results": [],
            }
        ],
    }
    (context.record.run_dir / "triage.json").write_text(json.dumps(triage))
    return workflow, context


def test_default_model_path_calls_planner_with_challenge_and_triage(tmp_path: Path) -> None:
    backend = ClaudeStubBackend([_planner_response()])
    workflow, context = _context(tmp_path, backend)

    outcome = asyncio.run(workflow.plan(context))

    assert outcome.target is RunState.SOLVE
    assert outcome.payload == {"hypotheses": 1, "planner_source": "model"}
    request = backend.requests[0]
    assert request.role == "planner"
    assert request.output_schema is not None
    assert request.context["challenge"]["title"] == "Model Planner Fixture"
    assert request.context["files"][0]["relative_path"] == "payload.bin"
    assert request.context["flag_policy"]["pattern"] == r"flag\{[^{}]+\}"
    persisted = json.loads((context.record.run_dir / "hypotheses.json").read_text())
    assert persisted[0]["id"] == "H-model"


def test_malformed_model_plan_uses_bounded_static_fallback(tmp_path: Path) -> None:
    backend = ClaudeStubBackend([ModelResponse(content="not-json")])
    workflow, context = _context(tmp_path, backend, allow_static_fallback=True)

    outcome = asyncio.run(workflow.plan(context))

    assert outcome.payload["planner_source"] == "static"
    assert any(
        event["event_type"] == "model.failure"
        for event in context.ledger.list(context.record.run_id)
    )


def test_malformed_model_plan_fails_when_fallback_disabled(tmp_path: Path) -> None:
    backend = ClaudeStubBackend([ModelResponse(content="not-json")])
    workflow, context = _context(tmp_path, backend, allow_static_fallback=False)

    with pytest.raises(ModelBackendError):
        asyncio.run(workflow.plan(context))


def test_model_call_budget_prevents_unbounded_planning(tmp_path: Path) -> None:
    backend = ClaudeStubBackend([_planner_response()])
    workflow, context = _context(
        tmp_path,
        backend,
        model_call_budget=1,
        allow_static_fallback=True,
    )
    context.ledger.append(
        context.record.run_id,
        "model.request",
        {"role": "planner", "request_index": 1},
        state=RunState.PLAN.value,
    )

    outcome = asyncio.run(workflow.plan(context))

    assert outcome.payload["planner_source"] == "static"
    assert backend.requests == []
