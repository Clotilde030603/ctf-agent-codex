"""Model-driven specialist backed by the controlled worker action loop."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ctf_agent.config import Settings
from ctf_agent.models.base import ModelBackend
from ctf_agent.models.factory import create_codex_backend
from ctf_agent.schemas import FlagCandidate, Hypothesis, SpecialistResult
from ctf_agent.workers import (
    CommandPolicy,
    LaneWorkspace,
    SharedModelCallBudget,
    WorkerBudget,
    WorkerCore,
    WorkerResult,
)

BackendFactory = Callable[[Settings, str, Path], ModelBackend]


class ModelSolverSpecialist:
    name = "model-solver"

    def __init__(
        self,
        settings: Settings,
        *,
        backend_factory: BackendFactory = create_codex_backend,
        local_test_mode: bool = False,
        allowed_argv0: set[str] | None = None,
        shared_model_budget: SharedModelCallBudget | None = None,
    ) -> None:
        self.settings = settings
        self.backend_factory = backend_factory
        self.local_test_mode = local_test_mode
        self.allowed_argv0 = allowed_argv0
        self.shared_model_budget = shared_model_budget

    def supports(self, _claim: str) -> bool:
        return True

    async def solve(
        self, hypothesis: Hypothesis, context: dict[str, object]
    ) -> SpecialistResult:
        run_dir = Path(str(context["run_dir"])).resolve()
        lane_dir = run_dir / "artifacts" / "lanes" / _lane_id(hypothesis)
        challenge_copy = lane_dir / "files"
        if not challenge_copy.exists():
            source_files = run_dir / "files"
            if source_files.is_dir():
                shutil.copytree(source_files, challenge_copy)
            else:
                challenge_copy.mkdir(parents=True)

        workspace = LaneWorkspace(lane_dir, challenge_files=run_dir / "files")
        policy = CommandPolicy(
            docker_image=self.settings.docker_image,
            local_test_mode=self.local_test_mode,
        )
        if self.allowed_argv0 is not None:
            policy.allowed_argv0 = self.allowed_argv0
        configured_model_budget = context.get(
            "model_call_budget", self.settings.model_call_budget
        )
        model_budget = (
            configured_model_budget
            if isinstance(configured_model_budget, int)
            else self.settings.model_call_budget
        )
        worker = WorkerCore(
            self.backend_factory(self.settings, "solver", lane_dir),
            workspace,
            budget=WorkerBudget(
                max_steps=self.settings.worker_max_steps,
                max_model_calls=min(
                    self.settings.worker_max_steps,
                    model_budget,
                ),
                max_commands=self.settings.worker_max_commands,
                max_wall_time_seconds=self.settings.worker_wall_time_seconds,
                command_timeout_seconds=self.settings.tool_timeout_seconds,
                max_no_progress_steps=self.settings.worker_no_progress_limit,
            ),
            policy=policy,
            shared_model_budget=self.shared_model_budget,
        )
        result = await worker.run(
            self._task(hypothesis),
            self._context(hypothesis, context, lane_dir),
        )
        report_path = lane_dir / "worker-result.json"
        report_path.write_text(
            json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        solve_path = next(
            (
                Path(path)
                for path in result.written_files
                if Path(path).name == "solve.py" and Path(path).is_file()
            ),
            None,
        )
        candidates: list[FlagCandidate] = []
        if result.status == "finished" and solve_path is not None:
            candidates = [
                candidate
                for candidate in result.flag_candidates
                if self._candidate_reproduced(result, candidate, solve_path)
            ]
        artifacts = _result_artifacts(run_dir, result.model_dump(mode="json"))
        artifacts.append(str(report_path.relative_to(run_dir)))
        if solve_path is not None:
            relative_solve = str(solve_path.relative_to(run_dir))
            if relative_solve not in artifacts:
                artifacts.append(relative_solve)

        confirmed = bool(candidates and solve_path is not None)
        return SpecialistResult(
            hypothesis_id=hypothesis.id,
            status="confirmed" if confirmed else "inconclusive",
            facts=result.facts,
            artifacts=artifacts,
            commands=[
                shlex.join(report.argv)
                for report in result.reports
                if report.action == "run" and report.argv
            ],
            reproduction_command="python3 solve.py" if solve_path else "",
            flag_candidates=candidates,
            next_action=result.message,
            confidence=(
                max(candidate.confidence for candidate in candidates)
                if candidates
                else 0.0
            ),
        )

    @staticmethod
    def _candidate_reproduced(
        result: WorkerResult,
        candidate: FlagCandidate,
        solve_path: Path,
    ) -> bool:
        try:
            declared = shlex.split(candidate.solver_command)
        except ValueError:
            return False
        if not declared or Path(declared[-1]).name != solve_path.name:
            return False
        for report in result.reports:
            if (
                report.action != "run"
                or report.status != "ok"
                or report.exit_code != 0
                or not report.stdout_artifact
                or not report.argv
                or Path(report.argv[-1]).name != solve_path.name
            ):
                continue
            stdout = Path(report.stdout_artifact)
            if stdout.is_file() and candidate.value in stdout.read_text(
                encoding="utf-8", errors="replace"
            ):
                return True
        return False

    @staticmethod
    def _task(hypothesis: Hypothesis) -> str:
        return (
            "Solve this explicitly authorized CTF hypothesis through controlled actions. "
            "Challenge files are copied under files/. Use only argv command actions or "
            "relative write_file actions. Create a data-dependent solve.py, execute it, "
            "record new facts with provenance, and finish only after emitting any flag "
            "candidates with source_artifact, source_location, derivation, and solver_command. "
            f"Hypothesis: {hypothesis.claim}"
        )

    @staticmethod
    def _context(
        hypothesis: Hypothesis,
        context: Mapping[str, object],
        lane_dir: Path,
    ) -> dict[str, Any]:
        return {
            "hypothesis": hypothesis.model_dump(mode="json"),
            "challenge": context.get("challenge", {}),
            "flag_policy": context.get("flag_policy", {}),
            "classification": context.get("classification", {}),
            "triage": context.get("triage", {}),
            "previous_attempts_and_failures": context.get(
                "previous_attempts_and_failures", []
            ),
            "lane_workspace": str(lane_dir),
            "challenge_copy": "files/",
            "network_policy": "no network; Docker commands use --network=none",
        }


def _lane_id(hypothesis: Hypothesis) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", hypothesis.id).strip("-") or "lane"
    fingerprint = hashlib.sha256(
        json.dumps(hypothesis.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()[:10]
    return f"{safe[:48]}-{fingerprint}"


def _result_artifacts(run_dir: Path, payload: Any) -> list[str]:
    paths: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith("_artifact") or key in {"written_path", "written_files"}:
                    visit(item)
                elif isinstance(item, dict | list):
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            path = Path(value)
            if path.is_absolute() and path.is_file() and run_dir in path.parents:
                relative = str(path.relative_to(run_dir))
                if relative not in paths:
                    paths.append(relative)

    visit(payload)
    return paths
