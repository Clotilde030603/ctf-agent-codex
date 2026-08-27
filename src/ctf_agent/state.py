"""Deterministic state machine and durable run checkpoints."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from ctf_agent.schemas import RunRecord, RunState

FORWARD_TRANSITIONS: dict[RunState, set[RunState]] = {
    RunState.AUTHENTICATE: {RunState.INGEST, RunState.FAILED},
    RunState.INGEST: {RunState.TRIAGE, RunState.FAILED},
    RunState.TRIAGE: {RunState.PLAN, RunState.FAILED},
    RunState.PLAN: {RunState.SOLVE, RunState.FAILED},
    RunState.SOLVE: {RunState.VERIFY, RunState.PLAN, RunState.FAILED},
    RunState.VERIFY: {RunState.SUBMIT, RunState.SOLVE, RunState.PLAN, RunState.FAILED},
    RunState.SUBMIT: {RunState.EVIDENCE, RunState.PLAN, RunState.TRIAGE, RunState.FAILED},
    RunState.EVIDENCE: {RunState.WRITEUP, RunState.REPRODUCE, RunState.FAILED},
    RunState.WRITEUP: {RunState.REPRODUCE, RunState.SOLVE, RunState.FAILED},
    RunState.REPRODUCE: {RunState.DONE, RunState.WRITEUP, RunState.SOLVE, RunState.FAILED},
    RunState.DONE: set(),
    RunState.FAILED: {RunState.AUTHENTICATE},
}


class InvalidTransition(ValueError):
    pass


def require_transition(current: RunState, target: RunState) -> None:
    if target not in FORWARD_TRANSITIONS[current]:
        raise InvalidTransition(f"invalid state transition: {current} -> {target}")


class StateStore:
    def __init__(self, database: Path) -> None:
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    challenge_url TEXT NOT NULL,
                    run_dir TEXT NOT NULL,
                    state TEXT NOT NULL,
                    auto_submit INTEGER NOT NULL,
                    writeup INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error TEXT
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS checkpoints (
                    run_id TEXT NOT NULL,
                    task_key TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    result_path TEXT,
                    PRIMARY KEY(run_id, task_key)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS rejected_candidates (
                    run_id TEXT NOT NULL,
                    value TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    rejected_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, value)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    value TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    submitted_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS submission_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    value TEXT NOT NULL,
                    status TEXT NOT NULL,
                    verdict TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )

    def create(self, record: RunRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    record.run_id,
                    record.challenge_url,
                    str(record.run_dir),
                    record.state.value,
                    record.auto_submit,
                    record.writeup,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                    record.last_error,
                ),
            )

    def load(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")
        return RunRecord(
            run_id=row["run_id"],
            challenge_url=row["challenge_url"],
            run_dir=Path(row["run_dir"]),
            state=RunState(row["state"]),
            auto_submit=bool(row["auto_submit"]),
            writeup=bool(row["writeup"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_error=row["last_error"],
        )

    def transition(self, run_id: str, target: RunState, error: str | None = None) -> RunRecord:
        current = self.load(run_id)
        require_transition(current.state, target)
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET state=?,updated_at=?,last_error=? WHERE run_id=?",
                (target.value, now, error, run_id),
            )
        return self.load(run_id)

    def checkpoint(self, run_id: str, task_key: str, result_path: Path | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?)",
                (
                    run_id,
                    task_key,
                    datetime.now(UTC).isoformat(),
                    str(result_path) if result_path else None,
                ),
            )

    def is_complete(self, run_id: str, task_key: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM checkpoints WHERE run_id=? AND task_key=?", (run_id, task_key)
            ).fetchone()
        return row is not None

    def reject_candidate(self, run_id: str, value: str, reason: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO rejected_candidates VALUES(?,?,?,?)",
                (run_id, value, reason, datetime.now(UTC).isoformat()),
            )

    def is_rejected(self, run_id: str, value: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM rejected_candidates WHERE run_id=? AND value=?", (run_id, value)
            ).fetchone()
        return row is not None

    def begin_submission(self, run_id: str, value: str, attempt_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO submission_attempts VALUES(?,?,?,?,?,?,?)",
                (attempt_id, run_id, value, "pending", None, now, now),
            )

    def pending_submission(self, run_id: str) -> tuple[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempt_id,value FROM submission_attempts "
                "WHERE run_id=? AND status='pending' ORDER BY created_at LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return str(row["attempt_id"]), str(row["value"])

    def record_submission(
        self,
        run_id: str,
        value: str,
        verdict: str,
        *,
        attempt_id: str | None = None,
    ) -> None:
        if attempt_id is None:
            seed = f"{run_id}\0{value}\0{datetime.now(UTC).isoformat()}"
            attempt_id = sha256(seed.encode()).hexdigest()
            self.begin_submission(run_id, value, attempt_id)
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                "UPDATE submission_attempts SET status='completed',verdict=?,updated_at=? "
                "WHERE attempt_id=?",
                (verdict, now, attempt_id),
            )
            connection.execute(
                "INSERT INTO submissions(run_id,value,verdict,submitted_at) VALUES(?,?,?,?)",
                (run_id, value, verdict, now),
            )

    def submission_count(self, run_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM submission_attempts WHERE run_id=?", (run_id,)
            ).fetchone()
        return int(row["count"])

    def submission_count_for_verdict(self, run_id: str, verdict: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM submissions WHERE run_id=? AND verdict=?",
                (run_id, verdict),
            ).fetchone()
        return int(row["count"])

    def latest_submission_verdict(self, run_id: str, value: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT verdict FROM submissions WHERE run_id=? AND value=? "
                "ORDER BY id DESC LIMIT 1",
                (run_id, value),
            ).fetchone()
        return str(row["verdict"]) if row is not None else None
