"""Append-only event ledger mirrored to SQLite and JSONL."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class EventLedger:
    def __init__(self, database: Path, jsonl: Path) -> None:
        self.database = database
        self.jsonl = jsonl
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.jsonl.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    state TEXT,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    idempotency_key TEXT,
                    UNIQUE(run_id, idempotency_key)
                )"""
            )

    def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        state: str | None = None,
        idempotency_key: str | None = None,
    ) -> int:
        created_at = datetime.now(UTC).isoformat()
        data = payload or {}
        encoded = json.dumps(data, sort_keys=True, default=str)
        with self._lock, self._connect() as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO events("
                    "run_id,event_type,state,created_at,payload,idempotency_key"
                    ") "
                    "VALUES(?,?,?,?,?,?)",
                    (run_id, event_type, state, created_at, encoded, idempotency_key),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("SQLite did not return an event id")
                event_id = cursor.lastrowid
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT id FROM events WHERE run_id=? AND idempotency_key=?",
                    (run_id, idempotency_key),
                ).fetchone()
                return int(row["id"])
            record = {
                "id": event_id,
                "run_id": run_id,
                "type": event_type,
                "state": state,
                "created_at": created_at,
                "payload": data,
                "idempotency_key": idempotency_key,
            }
            with self.jsonl.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        return event_id

    def list(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]
