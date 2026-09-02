"""SQLite persistence and migration for isolated lane checkpoints."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from ctf_agent.lanes.model import LaneCheckpoint
from ctf_agent.security import protect_file, redact_persisted_value


@dataclass(frozen=True, slots=True)
class CorruptLaneCheckpointError(RuntimeError):
    lane_id: str
    reason: str

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.lane_id, self.reason)

    def __str__(self) -> str:
        return (
            f"lane checkpoint {self.lane_id!r} is corrupt; "
            f"explicit reset required: {self.reason}"
        )


@dataclass(frozen=True, slots=True)
class LaneCheckpointConflictError(RuntimeError):
    lane_id: str
    reason: str

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.lane_id, self.reason)

    def __str__(self) -> str:
        return f"lane checkpoint conflict for {self.lane_id!r}: {self.reason}"


def initialize_lane_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS lane_checkpoints (
            lane_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            workspace_generation INTEGER NOT NULL,
            compatibility_fingerprint TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1
        )"""
    )
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(lane_checkpoints)")
    }
    if "revision" not in columns:
        connection.execute(
            "ALTER TABLE lane_checkpoints ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS lane_checkpoints_run_id_idx "
        "ON lane_checkpoints(run_id, lane_id)"
    )


class LaneCheckpointStore:
    """Durable per-lane truth with compatibility-aware generation resets."""

    def __init__(
        self,
        database: Path,
        event_observer: Callable[[str, Mapping[str, str | int]], None] | None = None,
    ) -> None:
        self.database = database
        self.event_observer = event_observer
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            initialize_lane_schema(connection)
        protect_file(self.database)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def load(self, lane_id: str) -> LaneCheckpoint | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json,revision FROM lane_checkpoints WHERE lane_id=?", (lane_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            checkpoint = LaneCheckpoint.model_validate(json.loads(str(row["payload_json"])))
            checkpoint = checkpoint.model_copy(update={"revision": int(row["revision"])})
        except (json.JSONDecodeError, ValidationError) as exc:
            raise CorruptLaneCheckpointError(lane_id, str(exc)) from exc
        if checkpoint.lane_id != lane_id:
            raise CorruptLaneCheckpointError(lane_id, "payload lane_id does not match row key")
        return checkpoint

    def list(self, run_id: str | None = None) -> tuple[LaneCheckpoint, ...]:
        query = "SELECT lane_id FROM lane_checkpoints"
        parameters: tuple[str, ...] = ()
        if run_id is not None:
            query += " WHERE run_id=?"
            parameters = (run_id,)
        query += " ORDER BY lane_id"
        with self._connect() as connection:
            lane_ids = [str(row["lane_id"]) for row in connection.execute(query, parameters)]
        checkpoints = [self.load(lane_id) for lane_id in lane_ids]
        return tuple(item for item in checkpoints if item is not None)

    def save(self, checkpoint: LaneCheckpoint, *, emit_event: bool = True) -> LaneCheckpoint:
        sanitized = LaneCheckpoint.model_validate(
            redact_persisted_value(checkpoint.model_dump(mode="json"))
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT run_id,workspace_generation,revision "
                "FROM lane_checkpoints WHERE lane_id=?",
                (sanitized.lane_id,),
            ).fetchone()
            if existing is None:
                if sanitized.revision != 0:
                    raise LaneCheckpointConflictError(
                        sanitized.lane_id, "initial revision is not zero"
                    )
                updated = sanitized.model_copy(
                    update={"revision": 1, "updated_at": datetime.now(UTC)}
                )
                connection.execute(
                    "INSERT INTO lane_checkpoints("
                    "lane_id,run_id,workspace_generation,compatibility_fingerprint,"
                    "payload_json,created_at,updated_at,revision) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        updated.lane_id,
                        updated.run_id,
                        updated.workspace_generation,
                        updated.compatibility_fingerprint,
                        updated.model_dump_json(),
                        updated.created_at.isoformat(),
                        updated.updated_at.isoformat(),
                        updated.revision,
                    ),
                )
                connection.commit()
                self._emit("lane.checkpoint.created", updated, "initial_checkpoint")
                return updated
            if str(existing["run_id"]) != sanitized.run_id:
                raise LaneCheckpointConflictError(sanitized.lane_id, "run_id changed")
            if int(existing["workspace_generation"]) > sanitized.workspace_generation:
                raise LaneCheckpointConflictError(
                    sanitized.lane_id, "generation moved backwards"
                )
            current_revision = int(existing["revision"])
            if current_revision != sanitized.revision:
                raise LaneCheckpointConflictError(sanitized.lane_id, "stale revision")
            updated = sanitized.model_copy(
                update={"revision": current_revision + 1, "updated_at": datetime.now(UTC)}
            )
            cursor = connection.execute(
                """UPDATE lane_checkpoints SET
                    workspace_generation=?, compatibility_fingerprint=?, payload_json=?,
                    updated_at=?, revision=? WHERE lane_id=? AND revision=?""",
                (
                    updated.workspace_generation,
                    updated.compatibility_fingerprint,
                    updated.model_dump_json(),
                    updated.updated_at.isoformat(),
                    updated.revision,
                    updated.lane_id,
                    current_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise LaneCheckpointConflictError(updated.lane_id, "stale revision")
        if emit_event:
            self._emit("lane.checkpoint.updated", updated, "checkpoint_saved")
        return updated

    def _emit(self, event_type: str, checkpoint: LaneCheckpoint, reason: str) -> None:
        if self.event_observer is not None:
            self.event_observer(event_type, {
                "lane_id": checkpoint.lane_id,
                "reason": reason,
                "revision": checkpoint.revision,
                "workspace_generation": checkpoint.workspace_generation,
            })

    def resume_or_reset(self, seed: LaneCheckpoint) -> tuple[LaneCheckpoint, bool]:
        current = self.load(seed.lane_id)
        if current is None:
            return self.save(seed), False
        if current.compatibility_fingerprint == seed.compatibility_fingerprint:
            resumed = current.model_copy(update={
                "hypothesis_id": seed.hypothesis_id,
                "hypothesis_revision": seed.hypothesis_revision,
                "hypothesis": seed.hypothesis,
                "restatement": seed.restatement,
            })
            self._emit("lane.checkpoint.resumed", resumed, "compatible_runtime_identity")
            return resumed, False
        replacement = seed.model_copy(
            update={
                "workspace_generation": current.workspace_generation + 1,
                "revision": current.revision,
                "created_at": datetime.now(UTC),
            }
        )
        updated = self.save(replacement, emit_event=False)
        self._emit("lane.checkpoint.reset", updated, "runtime_identity_incompatible")
        return updated, True

    def reset(self, lane_id: str, replacement: LaneCheckpoint) -> LaneCheckpoint:
        """Explicitly replace even an unreadable checkpoint with a new generation."""
        if replacement.lane_id != lane_id:
            raise LaneCheckpointConflictError(lane_id, "replacement lane_id differs")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT workspace_generation,revision FROM lane_checkpoints WHERE lane_id=?",
                (lane_id,),
            ).fetchone()
        generation = int(row["workspace_generation"]) + 1 if row is not None else 1
        revision = int(row["revision"]) if row is not None else 0
        reset = replacement.model_copy(
            update={
                "workspace_generation": generation,
                "revision": revision,
                "created_at": datetime.now(UTC),
            }
        )
        return self.save(reset)
