"""Controller-verified novelty accounting for model budget extensions."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from ctf_agent.budget_types import ArtifactProgress, BudgetNotFoundError, ProgressEvidence


class BudgetProgressStore:
    """Atomically consume novel progress identities when extending a budget."""

    def __init__(self, database: Path, run_id: str) -> None:
        self.database = database
        self.run_id = run_id

    def extend(self, evidence: ProgressEvidence) -> int:
        identities = verified_evidence_identities(evidence)
        if not identities:
            return 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            novel = tuple(
                identity
                for identity in identities
                if connection.execute(
                    "SELECT 1 FROM model_budget_progress "
                    "WHERE run_id=? AND evidence_identity=?",
                    (self.run_id, identity),
                ).fetchone()
                is None
            )
            if not novel:
                connection.commit()
                return 0
            row = connection.execute(
                "SELECT active_limit,hard_limit,extension_count,max_extensions,"
                "extension_size FROM model_budget_snapshots WHERE run_id=?",
                (self.run_id,),
            ).fetchone()
            if row is None:
                raise BudgetNotFoundError(f"run:{self.run_id}")
            if (
                int(row["extension_count"]) >= int(row["max_extensions"])
                or int(row["active_limit"]) >= int(row["hard_limit"])
            ):
                connection.commit()
                return 0
            added = min(
                int(row["extension_size"]),
                int(row["hard_limit"]) - int(row["active_limit"]),
            )
            now = datetime.now(UTC).isoformat()
            connection.executemany(
                "INSERT INTO model_budget_progress VALUES(?,?,?)",
                ((self.run_id, identity, now) for identity in novel),
            )
            connection.execute(
                "UPDATE model_budget_snapshots SET active_limit=active_limit+?,"
                "extension_count=extension_count+1,updated_at=? WHERE run_id=?",
                (added, now, self.run_id),
            )
            connection.execute(
                "INSERT INTO model_budget_role_totals(run_id,role,extended) "
                "VALUES(?,?,?) ON CONFLICT(run_id,role) DO UPDATE SET "
                "extended=extended+excluded.extended",
                (self.run_id, evidence.role.value, added),
            )
            connection.commit()
        return added

    @staticmethod
    def _artifact_matches(artifact: ArtifactProgress) -> bool:
        return (
            BudgetProgressStore._valid_sha256(artifact.content_sha256)
            and artifact.path.is_file()
            and sha256(artifact.path.read_bytes()).hexdigest() == artifact.content_sha256
        )

    @staticmethod
    def _valid_sha256(value: str) -> bool:
        return len(value) == 64 and all(
            character in "0123456789abcdef" for character in value
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection


def verified_evidence_identities(evidence: ProgressEvidence) -> tuple[str, ...]:
    """Return only identities whose controller-verifiable proof still matches."""
    identities = {
        f"fact:{fact.evidence_sha256}"
        for fact in evidence.facts
        if fact.status == "validated"
        and fact.evidence_sha256 == sha256(fact.fact.encode()).hexdigest()
    }
    identities.update(
        f"artifact:{artifact.content_sha256}"
        for artifact in evidence.artifacts
        if BudgetProgressStore._artifact_matches(artifact)
    )
    identities.update(
        f"candidate:{receipt.candidate_sha256}"
        for receipt in evidence.candidates
        if BudgetProgressStore._valid_sha256(receipt.candidate_sha256)
    )
    return tuple(sorted(identities))
