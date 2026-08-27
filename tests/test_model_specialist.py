from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path
from typing import Any

from ctf_agent.config import Settings
from ctf_agent.models.base import ModelBackend, ModelRequest, ModelResponse
from ctf_agent.schemas import Hypothesis
from ctf_agent.specialists.model import ModelSolverSpecialist


class QueueBackend:
    def __init__(self, decisions: list[dict[str, Any]]) -> None:
        self.decisions = list(decisions)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(content=json.dumps(self.decisions.pop(0)))


def _hypothesis() -> Hypothesis:
    return Hypothesis(
        id="crypto lane 1",
        claim="decode the base64 payload",
        supporting_evidence=["payload.txt contains base64"],
        expected_signal="flag plaintext",
        cost="low",
        confidence=0.8,
        required_tools=["python"],
        kill_condition="decode fails",
        success_condition="solver prints a flag",
    )


def test_model_specialist_writes_runs_and_reports_solver(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    files = run_dir / "files"
    files.mkdir(parents=True)
    encoded = base64.b64encode(b"flag{model_worker}").decode()
    (files / "payload.txt").write_text(encoded)
    solve_source = (
        "from pathlib import Path\n"
        "import base64\n"
        "print(base64.b64decode(Path('files/payload.txt').read_text()).decode())\n"
    )
    candidate = {
        "value": "flag{model_worker}",
        "source_artifact": "files/payload.txt",
        "source_location": "base64 decoded bytes",
        "derivation": ["base64 decode"],
        "solver_command": "python3 solve.py",
        "confidence": 0.9,
    }
    backend = QueueBackend(
        [
            {
                "action": "write_file",
                "path": "solve.py",
                "content": solve_source,
                "message": "created solver",
                "facts": ["payload is base64"],
            },
            {
                "action": "run",
                "argv": [sys.executable, "solve.py"],
                "message": "replayed solver",
                "facts": ["decoded output matches the flag policy"],
                "flag_candidates": [candidate],
            },
            {
                "action": "finish",
                "message": "candidate reproduced",
                "flag_candidates": [candidate],
            },
        ]
    )

    def backend_factory(
        settings: Settings, role: str, cwd: Path
    ) -> ModelBackend:
        assert role == "solver"
        assert cwd.name.startswith("crypto-lane-1-")
        return backend

    specialist = ModelSolverSpecialist(
        Settings(
            backend="codex",
            runs_dir=tmp_path / "runs",
            worker_max_steps=4,
            worker_max_commands=2,
        ),
        backend_factory=backend_factory,
        local_test_mode=True,
        allowed_argv0={Path(sys.executable).name},
    )

    result = asyncio.run(
        specialist.solve(
            _hypothesis(),
            {
                "run_dir": str(run_dir),
                "challenge": {"title": "Base64"},
                "flag_policy": {"pattern": r"flag\{[^{}]+\}"},
                "classification": {"primary_category": "crypto-binary"},
                "triage": {"files": [{"relative_path": "payload.txt"}]},
            },
        )
    )

    assert result.status == "confirmed"
    assert [item.value for item in result.flag_candidates] == ["flag{model_worker}"]
    assert result.commands == [f"{sys.executable} solve.py"]
    assert any(path.endswith("solve.py") for path in result.artifacts)
    assert any(path.endswith("worker-result.json") for path in result.artifacts)
    assert backend.requests[0].output_schema is not None


def test_model_specialist_returns_inconclusive_on_malformed_decision(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "files").mkdir(parents=True)
    backend = QueueBackend([{"not": "a worker decision"}])
    specialist = ModelSolverSpecialist(
        Settings(backend="codex", runs_dir=tmp_path / "runs"),
        backend_factory=lambda _settings, _role, _cwd: backend,
        local_test_mode=True,
    )

    result = asyncio.run(
        specialist.solve(_hypothesis(), {"run_dir": str(run_dir), "triage": {}})
    )

    assert result.status == "inconclusive"
    assert "model decision failed" in result.next_action
    assert result.flag_candidates == []


def test_model_specialist_rejects_candidate_from_failed_command(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "files").mkdir(parents=True)
    candidate = {
        "value": "flag{must_not_promote}",
        "source_artifact": "files/payload.txt",
        "source_location": "stdout",
        "derivation": ["failed command"],
        "solver_command": "python3 solve.py",
        "confidence": 0.9,
    }
    backend = QueueBackend(
        [
            {
                "action": "write_file",
                "path": "solve.py",
                "content": "print('flag{must_not_promote}')\n",
            },
            {
                "action": "run",
                "argv": ["curl", "https://outside.invalid"],
                "flag_candidates": [candidate],
            },
            {
                "action": "finish",
                "message": "done",
                "flag_candidates": [candidate],
            },
        ]
    )
    specialist = ModelSolverSpecialist(
        Settings(backend="codex", runs_dir=tmp_path / "runs", worker_max_steps=4),
        backend_factory=lambda _settings, _role, _cwd: backend,
        local_test_mode=True,
        allowed_argv0={Path(sys.executable).name},
    )

    result = asyncio.run(
        specialist.solve(_hypothesis(), {"run_dir": str(run_dir), "triage": {}})
    )

    assert result.status == "inconclusive"
    assert result.flag_candidates == []


def test_model_specialist_rejects_candidate_from_nonzero_solver(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "files").mkdir(parents=True)
    candidate = {
        "value": "flag{exit_one}",
        "source_artifact": "files/payload.txt",
        "source_location": "stdout",
        "derivation": ["nonzero solver"],
        "solver_command": "python3 solve.py",
        "confidence": 0.9,
    }
    backend = QueueBackend(
        [
            {
                "action": "write_file",
                "path": "solve.py",
                "content": "print('flag{exit_one}')\nraise SystemExit(1)\n",
            },
            {
                "action": "run",
                "argv": [sys.executable, "solve.py"],
                "flag_candidates": [candidate],
            },
            {"action": "finish", "flag_candidates": [candidate]},
        ]
    )
    specialist = ModelSolverSpecialist(
        Settings(backend="codex", runs_dir=tmp_path / "runs", worker_max_steps=4),
        backend_factory=lambda _settings, _role, _cwd: backend,
        local_test_mode=True,
        allowed_argv0={Path(sys.executable).name},
    )

    result = asyncio.run(
        specialist.solve(_hypothesis(), {"run_dir": str(run_dir), "triage": {}})
    )

    assert result.status == "inconclusive"
    assert result.flag_candidates == []
