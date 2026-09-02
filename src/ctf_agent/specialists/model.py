"""Model-driven specialist backed by the controlled worker action loop."""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from ctf_agent.auth_broker import AuthSessionBroker
from ctf_agent.budget_types import ModelBudgetLeaser
from ctf_agent.config import Settings
from ctf_agent.ingestion.session import ScopedAsyncSession
from ctf_agent.lanes import LaneCheckpoint, LaneRunResult, LaneStatus
from ctf_agent.models.base import ModelBackend
from ctf_agent.models.factory import create_codex_backend
from ctf_agent.reproduction import controller_reproduction_spec
from ctf_agent.schemas import FlagCandidate, Hypothesis, SpecialistResult
from ctf_agent.security import protect_file, redact_persisted_value
from ctf_agent.specialists.artifacts import result_artifacts
from ctf_agent.specialists.model_context import (
    checkpoint_seed,
    http_session,
    task,
    worker_context,
)
from ctf_agent.specialists.model_lane import prepare_lane
from ctf_agent.specialists.promotion import (
    PromotionAuthority,
    successful_argv,
    successful_argv_from_artifacts,
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
        model_budget: ModelBudgetLeaser | None = None,
        auth_broker: AuthSessionBroker | None = None,
        worker_failpoint: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.backend_factory = backend_factory
        self.local_test_mode = local_test_mode
        self.allowed_argv0 = allowed_argv0
        self.model_budget = model_budget
        self.auth_broker = auth_broker
        self.worker_failpoint = worker_failpoint

    def supports(self, _claim: str) -> bool:
        return True

    async def solve(
        self, hypothesis: Hypothesis, context: dict[str, object]
    ) -> SpecialistResult:
        """Run bounded durable slices until this invocation reaches a terminal state."""
        remaining = self.settings.worker_max_steps
        outcome: LaneRunResult | None = None
        commands: list[str] = []
        artifacts: list[str] = []
        while remaining > 0:
            slice_steps = min(2, remaining)
            outcome = await self.run_slice(hypothesis, context, max_steps=slice_steps)
            remaining -= slice_steps
            commands.extend(outcome.specialist_result.commands)
            artifacts.extend(outcome.specialist_result.artifacts)
            if outcome.status is not LaneStatus.PROGRESS:
                return outcome.specialist_result.model_copy(
                    update={
                        "commands": list(dict.fromkeys(commands)),
                        "artifacts": list(dict.fromkeys(artifacts)),
                    }
                )
        assert outcome is not None
        return outcome.specialist_result.model_copy(
            update={
                "commands": list(dict.fromkeys(commands)),
                "artifacts": list(dict.fromkeys(artifacts)),
            }
        )

    async def run_slice(
        self,
        hypothesis: Hypothesis,
        context: dict[str, object],
        *,
        max_steps: int | None = None,
        deadline: datetime | None = None,
    ) -> LaneRunResult:
        """Execute one bounded lane slice and commit its continuation checkpoint."""
        lane = prepare_lane(self, hypothesis, context)
        try:
            worker_slice = await lane.worker.run_slice(
                self._task(),
                self._context(hypothesis, context, lane.lane_dir),
                checkpoint=lane.checkpoint,
                max_steps=max_steps,
                deadline=deadline,
            )
            result = worker_slice.result
        finally:
            if lane.http_session is not None:
                await lane.http_session.aclose()
        report_path = lane.lane_dir / "worker-result.json"
        report_path.write_text(
            json.dumps(
                redact_persisted_value(result.model_dump(mode="json")),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        protect_file(report_path)

        solve_candidate = lane.lane_dir / "solve.py"
        solve_path: Path | None = solve_candidate if solve_candidate.is_file() else None
        candidates: list[FlagCandidate] = []
        receipt_store = lane.worker.execution_receipts
        if (
            result.status == "finished"
            and solve_path is not None
            and receipt_store is not None
        ):
            authority = PromotionAuthority(receipt_store, lane.checkpoint.lane_id, solve_path)
            for candidate in result.flag_candidates:
                successful = successful_argv(
                    result, candidate, authority
                ) or successful_argv_from_artifacts(
                    candidate, worker_slice.checkpoint.artifacts, authority
                )
                if successful is None:
                    continue
                candidate = candidate.model_copy(
                    update={
                        "reproduction_spec": controller_reproduction_spec(
                            lane.run_dir,
                            lane.lane_dir,
                            successful,
                            requires_auth_handle=bool(
                                lane.http_session is not None
                                and lane.http_session.authenticated
                            ),
                        )
                    }
                )
                candidates.append(candidate)
        artifacts = result_artifacts(lane.run_dir, result.model_dump(mode="json"))
        artifacts.append(str(report_path.relative_to(lane.run_dir)))
        if solve_path is not None:
            relative_solve = str(solve_path.relative_to(lane.run_dir))
            if relative_solve not in artifacts:
                artifacts.append(relative_solve)

        confirmed = bool(candidates and solve_path is not None)
        specialist_result = SpecialistResult(
            hypothesis_id=hypothesis.id,
            status="confirmed" if confirmed else "inconclusive",
            facts=list(worker_slice.checkpoint.verified_facts),
            artifacts=artifacts,
            commands=[
                shlex.join(report.argv)
                for report in result.reports
                if report.action == "run" and report.argv
            ],
            reproduction_command=(
                shlex.join(candidates[0].reproduction_spec.argv)
                if candidates and candidates[0].reproduction_spec is not None
                else "python3 solve.py" if solve_path else ""
            ),
            flag_candidates=candidates,
            next_action=result.message,
            confidence=(
                max(candidate.confidence for candidate in candidates)
                if candidates
                else 0.0
            ),
        )
        status = LaneStatus.SOLVED if confirmed else worker_slice.status
        if status is LaneStatus.SOLVED and not confirmed:
            status = LaneStatus.STALLED
        if self.worker_failpoint is not None:
            self.worker_failpoint("checkpoint_save")
        committed = lane.store.save(
            worker_slice.checkpoint.model_copy(
                update={
                    "status": status,
                    "artifacts": tuple(
                        dict.fromkeys((*worker_slice.checkpoint.artifacts, *artifacts))
                    ),
                }
            )
        )
        return LaneRunResult(
            status=status,
            checkpoint=committed,
            specialist_result=specialist_result,
        )

    def _checkpoint_seed(
        self,
        run_id: str,
        lane_id: str,
        hypothesis: Hypothesis,
        context: Mapping[str, object],
    ) -> LaneCheckpoint:
        return checkpoint_seed(self, run_id, lane_id, hypothesis, context)

    @staticmethod
    def _task() -> str:
        return task()

    @staticmethod
    def _context(
        hypothesis: Hypothesis,
        context: Mapping[str, object],
        lane_dir: Path,
    ) -> dict[str, Any]:
        return worker_context(hypothesis, context, lane_dir)

    def _http_session(self, context: Mapping[str, object]) -> ScopedAsyncSession | None:
        return http_session(self, context)
