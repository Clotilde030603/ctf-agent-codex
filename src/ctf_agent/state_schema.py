"""SQLite schema initialization and migrations for durable run state."""

import sqlite3

from ctf_agent.budget_schema import initialize_budget_schema
from ctf_agent.lanes.store import initialize_lane_schema

SCHEMA_VERSION = 7


def initialize_state_schema(connection: sqlite3.Connection) -> None:
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
        """CREATE TABLE IF NOT EXISTS reauthentication_intents (
            run_id TEXT PRIMARY KEY,
            return_state TEXT NOT NULL,
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
    initialize_budget_schema(connection)
    initialize_lane_schema(connection)
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
