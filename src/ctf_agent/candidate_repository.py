"""Verified and rejected candidate persistence."""

import hashlib
import json
from datetime import UTC, datetime

from pydantic import ValidationError

from ctf_agent.schemas import VerifiedCandidateRecord
from ctf_agent.security import redact_persisted_value
from ctf_agent.state_repository import SqliteRepository


class CorruptVerifiedCandidateError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)

    def __str__(self) -> str:
        return f"invalid persisted verification record: {self.reason}"


class CandidateRepository(SqliteRepository):
    def save_verified_candidate(self, record: VerifiedCandidateRecord) -> None:
        sanitized = VerifiedCandidateRecord.model_validate(
            redact_persisted_value(record.model_dump(mode="json"))
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO verified_candidates VALUES(?,?,?,?,?,?,?,?)",
                (
                    sanitized.run_id,
                    sanitized.candidate.model_dump_json(),
                    sanitized.solver_sha256,
                    sanitized.source_artifact,
                    sanitized.source_artifact_sha256,
                    sanitized.verified_at.isoformat(),
                    sanitized.valid,
                    sanitized.invalidation_reason,
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
            raise CorruptVerifiedCandidateError(str(exc)) from exc

    def invalidate_verified_candidate(self, run_id: str, reason: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE verified_candidates SET valid=0,invalidation_reason=? WHERE run_id=?",
                (redact_persisted_value(reason), run_id),
            )

    def reject_candidate(self, run_id: str, value: str, reason: str) -> None:
        value_identity = hashlib.sha256(value.encode()).hexdigest()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO rejected_candidates VALUES(?,?,?,?)",
                (
                    run_id,
                    value_identity,
                    redact_persisted_value(reason),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def is_rejected(self, run_id: str, value: str) -> bool:
        value_identity = hashlib.sha256(value.encode()).hexdigest()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM rejected_candidates WHERE run_id=? AND value IN (?,?)",
                (run_id, value_identity, value),
            ).fetchone()
        return row is not None
