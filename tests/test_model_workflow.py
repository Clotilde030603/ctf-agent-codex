from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import pytest

from ctf_agent.config import Settings
from ctf_agent.models.base import ModelBackend, ModelBackendError, ModelResponse
from ctf_agent.models.claude import ClaudeStubBackend
from ctf_agent.schemas import Challenge, FlagPolicy, Hypothesis, RunState
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


def test_model_solver_worker_is_used_when_preflight_finds_no_flag(tmp_path: Path) -> None:
    planner_backend = ClaudeStubBackend([_planner_response()])
    solver_source = (
        "from pathlib import Path\n"
        "import base64\n"
        "print(base64.b64decode(Path('files/payload.txt').read_text()).decode())\n"
    )
    candidate = {
        "value": "flag{workflow_model_worker}",
        "source_artifact": "files/payload.txt",
        "source_location": "base64 decoded bytes",
        "derivation": ["base64 decode"],
        "solver_command": "python3 solve.py",
        "confidence": 0.91,
    }
    solver_backend = ClaudeStubBackend(
        [
            json.dumps(
                {
                    "action": "write_file",
                    "path": "solve.py",
                    "content": solver_source,
                    "facts": ["payload is base64"],
                }
            ),
            json.dumps(
                {
                    "action": "run",
                    "argv": [sys.executable, "solve.py"],
                    "facts": ["decoded output matches flag policy"],
                    "flag_candidates": [candidate],
                }
            ),
            json.dumps(
                {
                    "action": "finish",
                    "message": "solver reproduced candidate",
                    "flag_candidates": [candidate],
                }
            ),
        ]
    )

    def solver_factory(
        settings: Settings, role: str, cwd: Path
    ) -> ModelBackend:
        assert role == "solver"
        assert cwd.parent.name == "lanes"
        return solver_backend

    workflow = AutonomousWorkflow(
        Settings(
            backend="codex",
            runs_dir=tmp_path / "runs",
            allow_local_reproduction=True,
            worker_max_steps=4,
            worker_max_commands=2,
            max_hypotheses=1,
            max_workers=1,
        ),
        planner_backend=planner_backend,
        solver_backend_factory=solver_factory,
        worker_local_test_mode=True,
        worker_allowed_argv0={Path(sys.executable).name},
    )
    controller = workflow.controller()
    context = controller.create_run(
        "https://ctf.test/challenges/8", auto_submit=False, writeup=False
    )
    context.values["challenge"] = Challenge(
        id="8",
        url="https://ctf.test/challenges/8",
        title="Encoded Model Fixture",
        category="rev",
        flag_policy=FlagPolicy(pattern=r"flag\{[^{}]+\}"),
    )
    encoded = base64.b64encode(b"flag{workflow_model_worker}").decode()
    (context.record.run_dir / "files" / "payload.txt").write_text(encoded)
    triage = {
        "classification": {
            "primary_category": "rev",
            "evidence": [{"reason": "base64-like payload"}],
        },
        "files": [
            {
                "relative_path": "payload.txt",
                "size": len(encoded),
                "mime": "text/plain",
                "magic": "ASCII text",
                "entropy": 4.0,
                "language": None,
                "indicators": [],
                "tool_results": [],
            }
        ],
    }
    context.values["triage"] = triage
    (context.record.run_dir / "triage.json").write_text(json.dumps(triage))

    asyncio.run(workflow.plan(context))
    outcome = asyncio.run(workflow.solve(context))

    assert outcome.target is RunState.VERIFY
    assert outcome.payload["stop_reason"] == "solved"
    assert (context.record.run_dir / "solve.py").is_file()
    persisted = json.loads(
        (context.record.run_dir / "artifacts" / "specialist-results.json").read_text()
    )
    assert any(
        candidate["value"] == "flag{workflow_model_worker}"
        for result in persisted
        for candidate in result["flag_candidates"]
    )
    assert len(solver_backend.requests) == 3
    model_events = [
        event
        for event in context.ledger.list(context.record.run_id)
        if event["event_type"] == "model.request"
    ]
    assert [event["payload"]["role"] for event in model_events] == [
        "planner",
        "solver",
        "solver",
        "solver",
    ]


def test_solver_lanes_cannot_exceed_shared_model_budget_after_plan(tmp_path: Path) -> None:
    planner_backend = ClaudeStubBackend([_planner_response()])
    solver_backend = ClaudeStubBackend(
        [
            json.dumps(
                {
                    "action": "write_file",
                    "path": "solve.py",
                    "content": "print('not enough budget')\n",
                }
            )
        ]
    )
    workflow = AutonomousWorkflow(
        Settings(
            backend="codex",
            runs_dir=tmp_path / "runs",
            model_call_budget=2,
            max_hypotheses=1,
            max_workers=1,
            worker_max_steps=4,
            allow_local_reproduction=True,
        ),
        planner_backend=planner_backend,
        solver_backend_factory=lambda _settings, _role, _cwd: solver_backend,
        worker_local_test_mode=True,
    )
    context = workflow.controller().create_run(
        "https://ctf.test/challenges/9", auto_submit=False, writeup=False
    )
    context.values["challenge"] = Challenge(
        id="9",
        url="https://ctf.test/challenges/9",
        title="Budget Fixture",
        flag_policy=FlagPolicy(pattern=r"flag\{[^{}]+\}"),
    )
    triage = {"classification": {"primary_category": "misc"}, "files": []}
    context.values["triage"] = triage
    (context.record.run_dir / "triage.json").write_text(json.dumps(triage))

    asyncio.run(workflow.plan(context))
    context.values.pop("hypotheses")
    outcome = asyncio.run(workflow.solve(context))

    assert outcome.target is RunState.PLAN
    assert len(solver_backend.requests) == 1
    model_events = [
        event
        for event in context.ledger.list(context.record.run_id)
        if event["event_type"] == "model.request"
    ]
    assert len(model_events) == 2


def test_codex_mode_runs_model_worker_even_when_preflight_has_candidate(
    tmp_path: Path,
) -> None:
    solver_backend = ClaudeStubBackend(
        [json.dumps({"action": "finish", "message": "reviewed preflight candidate"})]
    )
    workflow = AutonomousWorkflow(
        Settings(
            backend="codex",
            runs_dir=tmp_path / "runs",
            max_hypotheses=1,
            max_workers=1,
            worker_max_steps=1,
        ),
        solver_backend_factory=lambda _settings, _role, _cwd: solver_backend,
        worker_local_test_mode=True,
    )
    context = workflow.controller().create_run(
        "https://ctf.test/challenges/preflight", auto_submit=False, writeup=False
    )
    context.values["challenge"] = Challenge(
        id="preflight",
        url="https://ctf.test/challenges/preflight",
        title="Preflight",
        flag_policy=FlagPolicy(pattern=r"flag\{[^{}]+\}"),
    )
    source = context.record.run_dir / "files" / "payload.txt"
    source.write_text("flag{preflight_candidate}\n", encoding="utf-8")
    hypothesis = {
        "id": "H1",
        "claim": "inspect direct artifact",
        "supporting_evidence": [],
        "expected_signal": "candidate",
        "cost": "low",
        "confidence": 0.5,
        "required_tools": [],
        "kill_condition": "none",
        "success_condition": "candidate",
    }
    context.values["hypotheses"] = [Hypothesis.model_validate(hypothesis)]
    triage = {
        "classification": {"primary_category": "misc"},
        "files": [
            {
                "path": str(source),
                "relative_path": "files/payload.txt",
                "indicators": [
                    {
                        "kind": "flag-like",
                        "value": "flag{preflight_candidate}",
                        "artifact_path": str(source),
                        "offset": 0,
                    }
                ],
            }
        ],
    }
    context.values["triage"] = triage

    outcome = asyncio.run(workflow.solve(context))

    assert outcome.target is RunState.VERIFY
    assert outcome.payload["stop_reason"] == "model_reviewed_preflight_candidate"
    assert len(solver_backend.requests) == 1
    assert solver_backend.requests[0].context["preflight_results"][0]["status"] == "confirmed"
