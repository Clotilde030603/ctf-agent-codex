"""Submission attempt and verdict persistence."""

from datetime import UTC, datetime
from hashlib import sha256

from ctf_agent.state_repository import SqliteRepository


class SubmissionRepository(SqliteRepository):
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
