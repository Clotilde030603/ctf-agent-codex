"""Deterministic state machine and durable run checkpoints."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from pydantic import ValidationError

from ctf_agent.config import RunSettingsSnapshot
from ctf_agent.schemas import RunRecord, RunState, VerifiedCandidateRecord

SCHEMA_VERSION = 3

FORWARD_TRANSITIONS: dict[RunState, set[RunState]] = {
    RunState.AUTHENTICATE: {RunState.INGEST, RunState.FAILED},
    RunState.INGEST: {RunState.TRIAGE, RunState.FAILED},
    RunState.TRIAGE: {RunState.PLAN, RunState.FAILED},
    RunState.PLAN: {RunState.SOLVE, RunState.FAILED},
    RunState.SOLVE: {RunState.VERIFY, RunState.PLAN, RunState.FAILED},
    RunState.VERIFY: {RunState.REPRODUCE, RunState.SOLVE, RunState.PLAN, RunState.FAILED},
    RunState.REPRODUCE: {RunState.SUBMIT, RunState.SOLVE, RunState.FAILED},
    RunState.SUBMIT: {
        RunState.AUTHENTICATE,
        RunState.EVIDENCE_PENDING,
        RunState.READY,
        RunState.PLAN,
        RunState.TRIAGE,
        RunState.FAILED,
    },
    RunState.EVIDENCE_PENDING: {
        RunState.WRITEUP_PENDING,
        RunState.DONE,
        RunState.DONE_WITH_WARNINGS,
        RunState.FAILED,
    },
    RunState.WRITEUP_PENDING: {
        RunState.DONE,
        RunState.DONE_WITH_WARNINGS,
        RunState.FAILED,
    },
    # Legacy states remain loadable and resume into the new post-Accepted flow.
    RunState.EVIDENCE: {
        RunState.WRITEUP_PENDING,
        RunState.DONE,
        RunState.DONE_WITH_WARNINGS,
        RunState.FAILED,
    },
    RunState.WRITEUP: {RunState.DONE, RunState.DONE_WITH_WARNINGS, RunState.FAILED},
    RunState.READY: set(),
    RunState.DONE: set(),
    RunState.DONE_WITH_WARNINGS: set(),
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
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"state database schema {version} is newer than supported {SCHEMA_VERSION}"
                )
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
            connection.execute(
                """CREATE TABLE IF NOT EXISTS run_settings (
                    run_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS verified_candidates (
                    run_id TEXT PRIMARY KEY,
                    candidate_json TEXT NOT NULL,
                    solver_sha256 TEXT NOT NULL,
                    source_artifact TEXT NOT NULL,
                    source_artifact_sha256 TEXT NOT NULL,
                    verified_at TEXT NOT NULL,
                    valid INTEGER NOT NULL,
                    invalidation_reason TEXT
                )"""
            )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def create(
        self,
        record: RunRecord,
        settings_snapshot: RunSettingsSnapshot | None = None,
    ) -> None:
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
            if settings_snapshot is not None:
                connection.execute(
                    "INSERT INTO run_settings VALUES(?,?,?,?)",
                    (
                        record.run_id,
                        settings_snapshot.schema_version,
                        settings_snapshot.model_dump_json(),
                        datetime.now(UTC).isoformat(),
                    ),
                )

    def load_settings_snapshot(self, run_id: str) -> RunSettingsSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT schema_version,payload_json FROM run_settings WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        if int(row["schema_version"]) != 1:
            raise RuntimeError(
                f"unsupported run settings snapshot schema: {row['schema_version']}"
            )
        try:
            payload = json.loads(str(row["payload_json"]))
            return RunSettingsSnapshot.model_validate(payload)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise RuntimeError(f"invalid persisted run settings snapshot: {exc}") from exc

    def save_verified_candidate(self, record: VerifiedCandidateRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO verified_candidates VALUES(?,?,?,?,?,?,?,?)",
                (
                    record.run_id,
                    record.candidate.model_dump_json(),
                    record.solver_sha256,
                    record.source_artifact,
                    record.source_artifact_sha256,
                    record.verified_at.isoformat(),
                    record.valid,
                    record.invalidation_reason,
                ),
            )

    def load_verified_candidate(self, run_id: str) -> VerifiedCandidateRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM verified_candidates WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            return VerifiedCandidateRecord.model_validate(
                {
                    "run_id": row["run_id"],
                    "candidate": json.loads(str(row["candidate_json"])),
                    "solver_sha256": row["solver_sha256"],
                    "source_artifact": row["source_artifact"],
                    "source_artifact_sha256": row["source_artifact_sha256"],
                    "verified_at": row["verified_at"],
                    "valid": bool(row["valid"]),
                    "invalidation_reason": row["invalidation_reason"],
                }
            )
        except (json.JSONDecodeError, ValidationError) as exc:
            raise RuntimeError(f"invalid persisted verification record: {exc}") from exc

    def invalidate_verified_candidate(self, run_id: str, reason: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE verified_candidates SET valid=0,invalidation_reason=? WHERE run_id=?",
                (reason, run_id),
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

    def record_recoverable_error(self, run_id: str, error: str) -> RunRecord:
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET updated_at=?,last_error=? WHERE run_id=?",
                (datetime.now(UTC).isoformat(), error, run_id),
            )
        return self.load(run_id)

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

    def complete_state(
        self,
        run_id: str,
        *,
        expected_state: RunState,
        target: RunState,
        task_key: str,
        error: str | None = None,
        result_path: Path | None = None,
    ) -> RunRecord:
        """Atomically checkpoint a completed handler and advance its state."""
        require_transition(expected_state, target)
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown run: {run_id}")
            current = RunState(row["state"])
            if current is not expected_state:
                raise InvalidTransition(
                    f"state changed before completion: expected {expected_state}, got {current}"
                )
            connection.execute(
                "INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?)",
                (
                    run_id,
                    task_key,
                    now,
                    str(result_path) if result_path else None,
                ),
            )
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

    def abandon_submission(self, attempt_id: str, verdict: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE submission_attempts SET status='abandoned',verdict=?,updated_at=? "
                "WHERE attempt_id=? AND status='pending'",
                (verdict, datetime.now(UTC).isoformat(), attempt_id),
            )

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
                "SELECT COUNT(*) AS count FROM submission_attempts "
                "WHERE run_id=? AND status!='abandoned'",
                (run_id,),
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

    def has_accepted_submission(self, run_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM submissions WHERE run_id=? AND verdict IN (?,?) LIMIT 1",
                (run_id, "accepted", "already_solved"),
            ).fetchone()
        return row is not None


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
