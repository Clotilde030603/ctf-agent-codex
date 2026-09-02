"""Scripted model backend for deterministic offline benchmark workflows."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ctf_agent.context_projector import ContextProjector, render_codex_prompt
from ctf_agent.models.base import ModelRequest, ModelResponse
from ctf_agent.workflow import AutonomousWorkflow


@dataclass(frozen=True, slots=True)
class OfflineBenchmarkBackend:
    """Return deterministic planner, worker, and blind-review decisions."""

    workflow: AutonomousWorkflow
    role: str
    cwd: Path

    async def complete(self, request: ModelRequest) -> ModelResponse:
        request = replace(request, context=_deterministic_context(request.context))
        match self.role:
            case "planner":
                content = json.dumps({"hypotheses": _hypotheses()})
            case "solver":
                content = json.dumps(self._solver_decision(request))
            case "reviewer":
                content = json.dumps({"findings": [self._review_finding()]})
            case unexpected:
                raise RuntimeError(f"unexpected offline benchmark role: {unexpected}")
        metadata: dict[str, object] = {}
        if self.workflow.settings.context_projection_enabled:
            projection = ContextProjector(
                self.workflow.settings.max_model_context_bytes,
                recent_report_limit=self.workflow.settings.context_recent_report_limit,
            ).project(request, render_codex_prompt)
            metadata["projection_manifest"] = projection.manifest.model_dump(mode="json")
        return ModelResponse(content=content, metadata=metadata)

    def _solver_decision(self, request: ModelRequest) -> dict[str, object]:
        solve_path = self.cwd / "solve.py"
        prepared = self.cwd / "prepared.txt"
        reports = request.context.get("recent_reports", [])
        latest_action = (
            str(reports[-1].get("action", ""))
            if isinstance(reports, list) and reports and isinstance(reports[-1], dict)
            else ""
        )
        if not solve_path.is_file():
            return {
                "action": "write_file",
                "path": "solve.py",
                "content": _solver_source(),
                "facts": ["solver reads the frozen source artifact"],
            }
        if self.workflow.settings.lane_continuity_enabled and not prepared.is_file():
            return {
                "action": "write_file",
                "path": "prepared.txt",
                "content": "controller-observed continuation\n",
                "facts": ["first lane quantum preserved a continuation artifact"],
            }
        candidate = _candidate(self.cwd)
        if latest_action == "run":
            return {
                "action": "finish",
                "message": "candidate reproduced by the controlled worker",
                "flag_candidates": [candidate],
            }
        return {
            "action": "run",
            "argv": ["python3", "solve.py"],
            "facts": ["solver output matches the challenge flag policy"],
            "flag_candidates": [candidate],
        }

    def _review_finding(self) -> dict[str, object]:
        source = _source_path(self.cwd)
        return {
            "candidate": _candidate(self.cwd)["value"],
            "source_artifact": f"files/{source.name}",
            "source_location": "line 1",
            "reproduction_command": "python3 solve.py",
            "evidence": ["blind reviewer re-derived the value from the copied source"],
        }


def _deterministic_context(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _deterministic_context(item)
            for key, item in value.items()
            if key != "duration_seconds"
        }
    if isinstance(value, list):
        return [_deterministic_context(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_deterministic_context(item) for item in value)
    return value


def _hypotheses() -> list[dict[str, object]]:
    return [
        {
            "id": f"H{index}",
            "claim": f"derive the artifact-backed candidate through path {index}",
            "supporting_evidence": ["one frozen local artifact is attached"],
            "expected_signal": "a data-dependent solver output",
            "cost": "low" if index <= 3 else "medium",
            "confidence": 0.8 - index / 20,
            "required_tools": ["python3"],
            "kill_condition": "the solver cannot reproduce from the copied artifact",
            "success_condition": "blind replay and reviewer derivation agree",
        }
        for index in range(1, 7)
    ]


def _solver_source() -> str:
    return (
        "from pathlib import Path\n"
        "source = next(Path('files').glob('*'))\n"
        "print('flag{' + source.read_text(encoding='utf-8').strip() + '}')\n"
    )


def _source_path(cwd: Path) -> Path:
    return next((cwd / "files").glob("*"))


def _candidate(cwd: Path) -> dict[str, object]:
    source = _source_path(cwd)
    return {
        "value": f"flag{{{source.read_text(encoding='utf-8').strip()}}}",
        "source_artifact": f"files/{source.name}",
        "source_location": "line 1",
        "derivation": ["read the frozen source through solve.py"],
        "solver_command": "python3 solve.py",
        "confidence": 1.0,
    }
