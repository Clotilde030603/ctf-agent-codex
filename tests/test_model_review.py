from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ctf_agent.config import Settings
from ctf_agent.models.base import ModelBackend, ModelRequest, ModelResponse
from ctf_agent.schemas import Challenge, FlagCandidate, FlagPolicy, RunState, SpecialistResult
from ctf_agent.verification.model_review import ModelBlindReviewer
from ctf_agent.workflow import AutonomousWorkflow


class RecordingBackend:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(content=json.dumps(self.payload))


def test_model_reviewer_derives_without_expected_candidate_in_request(tmp_path: Path) -> None:
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "payload.txt").write_text("flag{blind_model}\n")
    (tmp_path / "solve.py").write_text(
        "from pathlib import Path\nprint(Path('files/payload.txt').read_text())\n"
    )
    backend = RecordingBackend(
        {
            "findings": [
                {
                    "candidate": "flag{blind_model}",
                    "source_artifact": "files/payload.txt",
                    "source_location": "line 1",
                    "reproduction_command": "python3 solve.py",
                    "evidence": ["solve.py reads files/payload.txt"],
                }
            ],
        }
    )

    def factory(_settings: Settings, role: str, cwd: Path) -> ModelBackend:
        assert role == "verifier"
        assert (cwd / "solve.py").is_file()
        return backend

    outcome = asyncio.run(
        ModelBlindReviewer(
            Settings(backend="codex"),
            tmp_path,
            {"pattern": r"flag\{[^{}]+\}"},
            backend_factory=factory,
        ).derive()
    )

    assert outcome.accepted is True
    assert outcome.derived_candidates == ("flag{blind_model}",)
    request = backend.requests[0]
    serialized = json.dumps(
        {"prompt": request.prompt, "system": request.system, "context": request.context}
    )
    assert "flag{blind_model}" not in serialized
    assert request.context["files"][0]["path"] == "files/payload.txt"


def test_model_reviewer_reports_empty_derivation(tmp_path: Path) -> None:
    (tmp_path / "files").mkdir()
    (tmp_path / "solve.py").write_text("print('no candidate')\n")
    backend = RecordingBackend({"findings": []})

    outcome = asyncio.run(
        ModelBlindReviewer(
            Settings(backend="codex"),
            tmp_path,
            {"pattern": r"flag\{[^{}]+\}"},
            backend_factory=lambda _settings, _role, _cwd: backend,
        ).derive()
    )

    assert outcome.accepted is False
    assert "no provenance-backed candidates" in outcome.reason


def test_codex_workflow_requires_reviewer_model_match(tmp_path: Path) -> None:
    (tmp_path / "unused").mkdir()
    backend = RecordingBackend(
        {
            "findings": [
                {
                    "candidate": "flag{reviewed}",
                    "source_artifact": "files/payload.txt",
                    "source_location": "line 1",
                    "reproduction_command": "python3 solve.py",
                    "evidence": ["solver output derived from files/payload.txt"],
                }
            ],
        }
    )
    workflow = AutonomousWorkflow(
        Settings(backend="codex", runs_dir=tmp_path / "runs"),
        reviewer_backend_factory=lambda _settings, _role, _cwd: backend,
    )
    context = workflow.controller().create_run(
        "https://ctf.test/challenges/review", auto_submit=False, writeup=False
    )
    payload = context.record.run_dir / "files" / "payload.txt"
    payload.write_text("flag{reviewed}\n", encoding="utf-8")
    (context.record.run_dir / "solve.py").write_text(
        "from pathlib import Path\nprint(Path('files/payload.txt').read_text())\n"
    )
    context.values["challenge"] = Challenge(
        id="review",
        url="https://ctf.test/challenges/review",
        title="Review",
        flag_policy=FlagPolicy(pattern=r"flag\{[^{}]+\}"),
    )
    context.values["specialist_results"] = [
        SpecialistResult(
            hypothesis_id="H1",
            status="confirmed",
            flag_candidates=[
                FlagCandidate(
                    value="flag{reviewed}",
                    source_artifact="files/payload.txt",
                    source_location="line 1",
                    derivation=["fixture"],
                    solver_command="python3 solve.py",
                )
            ],
        )
    ]

    outcome = asyncio.run(workflow.verify(context))

    assert outcome.target is RunState.REPRODUCE
    verified = context.values["candidate"]
    assert isinstance(verified, FlagCandidate)
    assert verified.independent_verified is True
    assert any(
        event["event_type"] == "model.completed"
        and event["payload"].get("role") == "verifier"
        for event in context.ledger.list(context.record.run_id)
    )
