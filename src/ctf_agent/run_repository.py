"""Run records, settings snapshots, and durable checkpoints."""

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from ctf_agent.config import RunSettingsSnapshot
from ctf_agent.schemas import RunRecord, RunState
from ctf_agent.security import redact_persisted_value
from ctf_agent.state_repository import SqliteRepository
from ctf_agent.state_transitions import InvalidTransition, require_transition


class UnsupportedSettingsSchemaError(RuntimeError):
    def __init__(self, schema_version: int) -> None:
        self.schema_version = schema_version
        super().__init__(schema_version)

    def __str__(self) -> str:
        return f"unsupported run settings snapshot schema: {self.schema_version}"


class CorruptRunSettingsError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)

    def __str__(self) -> str:
        return f"invalid persisted run settings snapshot: {self.reason}"


class RunNotFoundError(KeyError):
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(run_id)

    def __str__(self) -> str:
        return f"unknown run: {self.run_id}"


class RunRepository(SqliteRepository):
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
                    redact_persisted_value(record.challenge_url),
                    str(record.run_dir),
                    record.state.value,
                    record.auto_submit,
                    record.writeup,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                    redact_persisted_value(record.last_error),
                ),
            )
            if settings_snapshot is not None:
                connection.execute(
                    "INSERT INTO run_settings VALUES(?,?,?,?)",
                    (
                        record.run_id,
                        settings_snapshot.schema_version,
                        RunSettingsSnapshot.model_validate(
                            redact_persisted_value(settings_snapshot.model_dump(mode="json"))
                        ).model_dump_json(),
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
        if int(row["schema_version"]) not in {1, 2}:
            raise UnsupportedSettingsSchemaError(int(row["schema_version"]))
        try:
            payload = json.loads(str(row["payload_json"]))
            return RunSettingsSnapshot.model_validate(payload)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise CorruptRunSettingsError(str(exc)) from exc

    def load(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise RunNotFoundError(run_id)
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

    def reauthentication_target(self, run_id: str) -> RunState | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT return_state FROM reauthentication_intents WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return None if row is None else RunState(row["return_state"])

    def begin_reauthentication(self, run_id: str, return_state: RunState) -> RunRecord:
        if return_state not in {RunState.SOLVE, RunState.VERIFY}:
            raise InvalidTransition(
                f"authentication cannot return to {return_state.value}"
            )
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise InvalidTransition("run disappeared during authentication routing")
            current = RunState(row["state"])
            require_transition(current, RunState.AUTHENTICATE)
            existing = connection.execute(
                "SELECT return_state FROM reauthentication_intents WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if existing is not None and existing["return_state"] != return_state.value:
                raise InvalidTransition("conflicting authentication return state")
            connection.execute(
                "INSERT OR REPLACE INTO reauthentication_intents VALUES(?,?,?)",
                (run_id, return_state.value, now),
            )
            connection.execute(
                "UPDATE runs SET state=?,updated_at=?,last_error=NULL WHERE run_id=?",
                (RunState.AUTHENTICATE.value, now, run_id),
            )
        return self.load(run_id)

    def transition(
        self, run_id: str, target: RunState, error: str | None = None
    ) -> RunRecord:
        current = self.load(run_id)
        require_transition(current.state, target)
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET state=?,updated_at=?,last_error=? WHERE run_id=?",
                (target.value, now, redact_persisted_value(error), run_id),
            )
        return self.load(run_id)

    def record_recoverable_error(self, run_id: str, error: str) -> RunRecord:
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET updated_at=?,last_error=? WHERE run_id=?",
                (
                    datetime.now(UTC).isoformat(),
                    redact_persisted_value(error),
                    run_id,
                ),
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
                raise RunNotFoundError(run_id)
            current = RunState(row["state"])
            if current is not expected_state:
                raise InvalidTransition(
                    f"state changed before completion: expected {expected_state}, got {current}"
                )
            clear_reauthentication = False
            if expected_state is RunState.AUTHENTICATE and target in {
                RunState.SOLVE,
                RunState.VERIFY,
            }:
                intent = connection.execute(
                    "SELECT return_state FROM reauthentication_intents WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if intent is None or intent["return_state"] != target.value:
                    raise InvalidTransition("authentication return state has no matching intent")
                clear_reauthentication = True
            connection.execute(
                "INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?)",
                (
                    run_id,
                    task_key,
                    now,
                    redact_persisted_value(str(result_path)) if result_path else None,
                ),
            )
            connection.execute(
                "UPDATE runs SET state=?,updated_at=?,last_error=? WHERE run_id=?",
                (target.value, now, redact_persisted_value(error), run_id),
            )
            if clear_reauthentication:
                connection.execute(
                    "DELETE FROM reauthentication_intents WHERE run_id=?", (run_id,)
                )
        return self.load(run_id)

    def checkpoint(
        self, run_id: str, task_key: str, result_path: Path | None = None
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?)",
                (
                    run_id,
                    task_key,
                    datetime.now(UTC).isoformat(),
                    redact_persisted_value(str(result_path)) if result_path else None,
                ),
            )

    def is_complete(self, run_id: str, task_key: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM checkpoints WHERE run_id=? AND task_key=?",
                (run_id, task_key),
            ).fetchone()
        return row is not None
