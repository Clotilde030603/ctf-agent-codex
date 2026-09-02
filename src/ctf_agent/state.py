"""Deterministic state machine and durable run checkpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ctf_agent.budget import ModelBudgetBroker
    from ctf_agent.budget_types import BudgetPolicy

from ctf_agent.candidate_repository import CandidateRepository
from ctf_agent.lanes.store import LaneCheckpointStore
from ctf_agent.run_repository import RunRepository
from ctf_agent.schemas import RunRecord, RunState
from ctf_agent.security import protect_file
from ctf_agent.state_repository import SqliteRepository
from ctf_agent.state_schema import SCHEMA_VERSION, initialize_state_schema
from ctf_agent.state_transitions import (
    FORWARD_TRANSITIONS,
    InvalidTransition,
    require_transition,
)
from ctf_agent.submission_repository import SubmissionRepository


class StateStore(RunRepository, CandidateRepository, SubmissionRepository):
    def __init__(self, database: Path) -> None:
        SqliteRepository.__init__(self, database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _initialize(self) -> None:
        with self._connect() as connection:
            initialize_state_schema(connection)
        protect_file(self.database)

    def prepare_evidence_retry(self, run_id: str) -> RunRecord:
        record = self.load(run_id)
        if not self.has_accepted_submission(run_id):
            raise InvalidTransition("evidence retry requires a durable Accepted verdict")
        allowed = {
            RunState.EVIDENCE_PENDING,
            RunState.WRITEUP_PENDING,
            RunState.DONE_WITH_WARNINGS,
            RunState.EVIDENCE,
            RunState.WRITEUP,
            RunState.FAILED,
        }
        if record.state not in allowed:
            raise InvalidTransition(
                f"evidence cannot be retried from state {record.state.value}"
            )
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET state=?,updated_at=?,last_error=NULL WHERE run_id=?",
                (RunState.EVIDENCE_PENDING.value, datetime.now(UTC).isoformat(), run_id),
            )
        return self.load(run_id)

    def lane_checkpoints(self) -> LaneCheckpointStore:
        return LaneCheckpointStore(self.database)

    def model_budget_broker(
        self, run_id: str, policy: BudgetPolicy
    ) -> ModelBudgetBroker:
        from ctf_agent.budget import ModelBudgetBroker

        return ModelBudgetBroker.create(self.database, run_id, policy)


def find_run_database(runs_dir: Path, run_id: str) -> Path:
    candidates = list(runs_dir.glob(f"**/*{run_id}*/state.db"))
    if not candidates:
        candidates = list(runs_dir.glob("**/state.db"))
    for database in candidates:
        try:
            StateStore(database).load(run_id)
        except KeyError:
            continue
        return database
    raise KeyError(f"run not found: {run_id}")


__all__ = [
    "FORWARD_TRANSITIONS",
    "SCHEMA_VERSION",
    "InvalidTransition",
    "StateStore",
    "find_run_database",
    "require_transition",
]
