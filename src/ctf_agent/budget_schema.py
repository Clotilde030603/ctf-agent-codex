"""SQLite schema bootstrap shared by state migration and public budget creation."""

from __future__ import annotations

import sqlite3


def initialize_budget_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS model_budget_snapshots (
            run_id TEXT PRIMARY KEY,
            initial_limit INTEGER NOT NULL,
            active_limit INTEGER NOT NULL,
            hard_limit INTEGER NOT NULL,
            verifier_floor INTEGER NOT NULL,
            planner_soft_limit INTEGER NOT NULL,
            max_extensions INTEGER NOT NULL,
            extension_size INTEGER NOT NULL,
            retry_reserve INTEGER NOT NULL DEFAULT 0,
            verifier_candidate_limit INTEGER NOT NULL DEFAULT 0,
            extension_count INTEGER NOT NULL,
            requested INTEGER NOT NULL,
            final_stop_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS model_budget_leases (
            lease_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            role TEXT NOT NULL,
            purpose TEXT NOT NULL,
            request_id TEXT NOT NULL,
            status TEXT NOT NULL,
            borrowed INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(run_id, request_id)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS model_budget_role_totals (
            run_id TEXT NOT NULL,
            role TEXT NOT NULL,
            requested INTEGER NOT NULL DEFAULT 0,
            extended INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(run_id, role)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS model_budget_progress (
            run_id TEXT NOT NULL,
            evidence_identity TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(run_id, evidence_identity)
        )"""
    )
    for column, definition in (
        ("retry_reserve", "INTEGER NOT NULL DEFAULT 0"),
        ("verifier_candidate_limit", "INTEGER NOT NULL DEFAULT 0"),
        ("final_stop_reason", "TEXT NOT NULL DEFAULT ''"),
    ):
        try:
            connection.execute(
                f"ALTER TABLE model_budget_snapshots ADD COLUMN {column} {definition}"
            )
        except sqlite3.OperationalError as error:
            if "duplicate column name" not in str(error):
                raise
    connection.execute(
        "CREATE INDEX IF NOT EXISTS model_budget_leases_run_status "
        "ON model_budget_leases(run_id,status,role)"
    )
