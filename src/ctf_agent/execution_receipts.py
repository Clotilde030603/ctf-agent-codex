"""Controller-owned receipts for successful canonical solver executions."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    """Exact controller-observed inputs and outputs bound by one receipt."""

    lane_id: str
    argv: Sequence[str]
    solver_path: Path
    stdout_path: Path
    stderr_path: Path
    command_fingerprint: str
    output_fingerprint: str


class ExecutionReceiptStore:
    """Persist command authority outside the model-writable lane workspace."""

    def __init__(self, database: Path) -> None:
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS execution_receipts (
                    receipt TEXT PRIMARY KEY,
                    lane_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    argv_json TEXT NOT NULL,
                    solver_sha256 TEXT NOT NULL,
                    stdout_sha256 TEXT NOT NULL,
                    stderr_sha256 TEXT NOT NULL,
                    command_fingerprint TEXT NOT NULL,
                    output_fingerprint TEXT NOT NULL,
                    UNIQUE(lane_id, sequence)
                )"""
            )
        self.database.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def issue(self, evidence: ExecutionEvidence) -> str:
        """Issue an opaque receipt bound to controller-observed execution bytes."""
        receipt = secrets.token_urlsafe(32)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM execution_receipts WHERE lane_id=?",
                (evidence.lane_id,),
            ).fetchone()
            assert row is not None
            sequence = int(row[0])
            connection.execute(
                "INSERT INTO execution_receipts VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    receipt,
                    evidence.lane_id,
                    sequence,
                    json.dumps(list(evidence.argv), separators=(",", ":")),
                    _sha256(evidence.solver_path),
                    _sha256(evidence.stdout_path),
                    _sha256(evidence.stderr_path),
                    evidence.command_fingerprint,
                    evidence.output_fingerprint,
                ),
            )
        return receipt

    def verifies(self, receipt: str, evidence: ExecutionEvidence) -> bool:
        """Match a receipt against current artifacts and exact execution identity."""
        if not all(
            path.is_file()
            for path in (evidence.solver_path, evidence.stdout_path, evidence.stderr_path)
        ):
            return False
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM execution_receipts WHERE receipt=? AND lane_id=?",
                (receipt, evidence.lane_id),
            ).fetchone()
        if row is None:
            return False
        return (
            str(row["argv_json"])
            == json.dumps(list(evidence.argv), separators=(",", ":"))
            and str(row["solver_sha256"]) == _sha256(evidence.solver_path)
            and str(row["stdout_sha256"]) == _sha256(evidence.stdout_path)
            and str(row["stderr_sha256"]) == _sha256(evidence.stderr_path)
            and str(row["command_fingerprint"]) == evidence.command_fingerprint
            and str(row["output_fingerprint"]) == evidence.output_fingerprint
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
