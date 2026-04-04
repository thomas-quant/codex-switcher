from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from codex_switch.automation_models import HandoffPhase, RateLimitSnapshot, UsageSource
from codex_switch.errors import AutomationDatabaseError
from codex_switch.fs import ensure_private_dir

SCHEMA_VERSION = 1
_HANDOFF_STATE_KEY = 1


@dataclass(slots=True, frozen=True)
class RateLimitRecord:
    alias: str
    limit_id: str | None
    limit_name: str
    observed_via: UsageSource
    plan_type: str | None
    primary_used_percent: float | None
    primary_resets_at: str | None
    primary_window_duration_mins: int | None
    secondary_used_percent: float | None
    secondary_resets_at: str | None
    secondary_window_duration_mins: int | None
    credits_has_credits: bool | None
    credits_unlimited: bool | None
    credits_balance: int | None
    observed_at: str


class AutomationStore:
    def __init__(self, db_file: Path) -> None:
        self._db_file = Path(db_file)

    def initialize(self) -> None:
        def initialize_schema(conn: sqlite3.Connection) -> None:
            self._ensure_schema(conn)

        self._run(initialize_schema)

    def upsert_rate_limit(self, snapshot: RateLimitSnapshot) -> None:
        def write_rate_limit(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO rate_limits (
                    alias,
                    limit_id,
                    limit_id_key,
                    limit_name,
                    observed_via,
                    plan_type,
                    primary_used_percent,
                    primary_resets_at,
                    primary_window_duration_mins,
                    secondary_used_percent,
                    secondary_resets_at,
                    secondary_window_duration_mins,
                    credits_has_credits,
                    credits_unlimited,
                    credits_balance,
                    observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(alias, limit_id_key) DO UPDATE SET
                    limit_id = excluded.limit_id,
                    limit_name = excluded.limit_name,
                    observed_via = excluded.observed_via,
                    plan_type = excluded.plan_type,
                    primary_used_percent = excluded.primary_used_percent,
                    primary_resets_at = excluded.primary_resets_at,
                    primary_window_duration_mins = excluded.primary_window_duration_mins,
                    secondary_used_percent = excluded.secondary_used_percent,
                    secondary_resets_at = excluded.secondary_resets_at,
                    secondary_window_duration_mins = excluded.secondary_window_duration_mins,
                    credits_has_credits = excluded.credits_has_credits,
                    credits_unlimited = excluded.credits_unlimited,
                    credits_balance = excluded.credits_balance,
                    observed_at = excluded.observed_at
                """,
                (
                    snapshot.alias,
                    snapshot.limit_id,
                    _limit_key(snapshot.limit_id),
                    snapshot.limit_name,
                    snapshot.observed_via.value,
                    snapshot.plan_type,
                    snapshot.primary_window.used_percent,
                    snapshot.primary_window.resets_at,
                    snapshot.primary_window.window_duration_mins,
                    snapshot.secondary_window.used_percent,
                    snapshot.secondary_window.resets_at,
                    snapshot.secondary_window.window_duration_mins,
                    _bool_to_int(snapshot.credits_has_credits),
                    _bool_to_int(snapshot.credits_unlimited),
                    snapshot.credits_balance,
                    snapshot.observed_at,
                ),
            )

        self._run(write_rate_limit)

    def list_rate_limits_for_alias(self, alias: str) -> list[RateLimitRecord]:
        def read_rate_limits(conn: sqlite3.Connection) -> list[RateLimitRecord]:
            rows = conn.execute(
                """
                SELECT
                    alias,
                    limit_id,
                    limit_name,
                    observed_via,
                    plan_type,
                    primary_used_percent,
                    primary_resets_at,
                    primary_window_duration_mins,
                    secondary_used_percent,
                    secondary_resets_at,
                    secondary_window_duration_mins,
                    credits_has_credits,
                    credits_unlimited,
                    credits_balance,
                    observed_at
                FROM rate_limits
                WHERE alias = ?
                ORDER BY observed_at DESC, limit_id_key ASC
                """,
                (alias,),
            ).fetchall()
            return [_row_to_rate_limit_record(row) for row in rows]

        return self._run(read_rate_limits)

    def set_handoff_state(
        self,
        thread_id: str,
        source_alias: str | None,
        target_alias: str | None,
        phase: HandoffPhase,
        reason: str | None,
        updated_at: str,
    ) -> None:
        def write_handoff_state(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO handoff_state (
                    singleton_key,
                    thread_id,
                    source_alias,
                    target_alias,
                    phase,
                    reason,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton_key) DO UPDATE SET
                    thread_id = excluded.thread_id,
                    source_alias = excluded.source_alias,
                    target_alias = excluded.target_alias,
                    phase = excluded.phase,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (
                    _HANDOFF_STATE_KEY,
                    thread_id,
                    source_alias,
                    target_alias,
                    phase.value,
                    reason,
                    updated_at,
                ),
            )

        self._run(write_handoff_state)

    def _run(self, callback):
        try:
            self._prepare_db_file()
            with sqlite3.connect(self._db_file) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = ON")
                self._ensure_schema(conn)
                result = callback(conn)
            return result
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise AutomationDatabaseError(f"Could not access {self._db_file}: {exc}") from exc

    def _prepare_db_file(self) -> None:
        if self._db_file.is_symlink():
            raise AutomationDatabaseError(f"Unsafe automation db path: {self._db_file} is a symlink")
        ensure_private_dir(self._db_file.parent, root=self._db_file.parent)
        if self._db_file.exists():
            os.chmod(self._db_file, 0o600)
            return
        fd = os.open(self._db_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rate_limits (
                alias TEXT NOT NULL,
                limit_id TEXT,
                limit_id_key TEXT NOT NULL,
                limit_name TEXT NOT NULL,
                observed_via TEXT NOT NULL,
                plan_type TEXT,
                primary_used_percent REAL,
                primary_resets_at TEXT,
                primary_window_duration_mins INTEGER,
                secondary_used_percent REAL,
                secondary_resets_at TEXT,
                secondary_window_duration_mins INTEGER,
                credits_has_credits INTEGER,
                credits_unlimited INTEGER,
                credits_balance INTEGER,
                observed_at TEXT NOT NULL,
                PRIMARY KEY (alias, limit_id_key)
            );

            CREATE INDEX IF NOT EXISTS idx_rate_limits_alias
                ON rate_limits(alias);

            CREATE TABLE IF NOT EXISTS handoff_state (
                singleton_key INTEGER PRIMARY KEY CHECK (singleton_key = 1),
                thread_id TEXT NOT NULL,
                source_alias TEXT,
                target_alias TEXT,
                phase TEXT NOT NULL,
                reason TEXT,
                updated_at TEXT NOT NULL
            );
            """
        )
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = ?",
            ("schema_version",),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO metadata (key, value)
                VALUES (?, ?)
                """,
                ("schema_version", str(SCHEMA_VERSION)),
            )
            return
        if row["value"] != str(SCHEMA_VERSION):
            raise AutomationDatabaseError(
                f"Unsupported automation schema version {row['value']}; expected {SCHEMA_VERSION}"
            )


def _limit_key(limit_id: str | None) -> str:
    return json.dumps(limit_id, separators=(",", ":"))


def _bool_to_int(value: bool | None) -> int | None:
    if value is None:
        return None
    return int(value)


def _int_to_bool(value: int | None) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _row_to_rate_limit_record(row: sqlite3.Row) -> RateLimitRecord:
    return RateLimitRecord(
        alias=row["alias"],
        limit_id=row["limit_id"],
        limit_name=row["limit_name"],
        observed_via=UsageSource(row["observed_via"]),
        plan_type=row["plan_type"],
        primary_used_percent=row["primary_used_percent"],
        primary_resets_at=row["primary_resets_at"],
        primary_window_duration_mins=row["primary_window_duration_mins"],
        secondary_used_percent=row["secondary_used_percent"],
        secondary_resets_at=row["secondary_resets_at"],
        secondary_window_duration_mins=row["secondary_window_duration_mins"],
        credits_has_credits=_int_to_bool(row["credits_has_credits"]),
        credits_unlimited=_int_to_bool(row["credits_unlimited"]),
        credits_balance=row["credits_balance"],
        observed_at=row["observed_at"],
    )
