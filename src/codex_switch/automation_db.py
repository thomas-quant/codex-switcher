from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from codex_switch.automation_models import HandoffPhase, RateLimitSnapshot, UsageSource
from codex_switch.errors import AutomationDatabaseError
from codex_switch.fs import ensure_private_dir

SCHEMA_VERSION = 3
_HANDOFF_STATE_KEY = 1

_ALIASES_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS aliases (
    alias TEXT PRIMARY KEY,
    account_email TEXT,
    account_plan_type TEXT,
    account_fingerprint TEXT,
    last_observed_at TEXT
);
"""

_RATE_LIMITS_CREATE_SQL = """
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
    credits_balance TEXT,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (alias, limit_id_key)
);
"""

_RATE_LIMITS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_rate_limits_alias
    ON rate_limits(alias);
"""

_SWITCH_EVENTS_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS switch_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT,
    from_alias TEXT,
    to_alias TEXT,
    trigger_type TEXT,
    trigger_limit_id TEXT,
    trigger_used_percent REAL,
    requested_at TEXT NOT NULL,
    switched_at TEXT,
    resumed_at TEXT,
    result TEXT NOT NULL,
    failure_message TEXT
);
"""

_SWITCH_EVENTS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_switch_events_requested_at
    ON switch_events(requested_at DESC, id DESC);
"""

_THREAD_RUNTIME_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS thread_runtime (
    thread_id TEXT PRIMARY KEY,
    cwd TEXT,
    model TEXT,
    current_alias TEXT,
    last_turn_id TEXT,
    last_known_status TEXT,
    safe_to_switch INTEGER NOT NULL,
    last_total_tokens INTEGER,
    last_seen_at TEXT NOT NULL
);
"""

_THREAD_RUNTIME_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_thread_runtime_last_seen_at
    ON thread_runtime(last_seen_at DESC, thread_id ASC);
"""

_THREAD_TURN_USAGE_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS thread_turn_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    last_input_tokens INTEGER,
    last_cached_input_tokens INTEGER,
    last_output_tokens INTEGER,
    last_reasoning_output_tokens INTEGER,
    last_total_tokens INTEGER,
    total_input_tokens INTEGER,
    total_cached_input_tokens INTEGER,
    total_output_tokens INTEGER,
    total_reasoning_output_tokens INTEGER,
    total_tokens INTEGER,
    observed_at TEXT NOT NULL
);
"""

_THREAD_TURN_USAGE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_thread_turn_usage_thread_observed_at
    ON thread_turn_usage(thread_id, observed_at DESC, id DESC);
"""


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
    credits_balance: str | None
    observed_at: str


@dataclass(slots=True, frozen=True)
class HandoffStateRecord:
    thread_id: str
    source_alias: str | None
    target_alias: str | None
    phase: HandoffPhase
    reason: str | None
    updated_at: str


@dataclass(slots=True, frozen=True)
class SwitchEventRecord:
    id: int
    thread_id: str | None
    from_alias: str | None
    to_alias: str | None
    trigger_type: str | None
    trigger_limit_id: str | None
    trigger_used_percent: float | None
    requested_at: str
    switched_at: str | None
    resumed_at: str | None
    result: str
    failure_message: str | None


@dataclass(slots=True, frozen=True)
class AliasRecord:
    alias: str
    account_email: str | None
    account_plan_type: str | None
    account_fingerprint: str | None
    last_observed_at: str | None


@dataclass(slots=True, frozen=True)
class ThreadRuntimeRecord:
    thread_id: str
    cwd: str | None
    model: str | None
    current_alias: str | None
    last_turn_id: str | None
    last_known_status: str | None
    safe_to_switch: bool
    last_total_tokens: int | None
    last_seen_at: str


@dataclass(slots=True, frozen=True)
class ThreadTurnUsageRecord:
    id: int
    thread_id: str
    turn_id: str
    last_input_tokens: int | None
    last_cached_input_tokens: int | None
    last_output_tokens: int | None
    last_reasoning_output_tokens: int | None
    last_total_tokens: int | None
    total_input_tokens: int | None
    total_cached_input_tokens: int | None
    total_output_tokens: int | None
    total_reasoning_output_tokens: int | None
    total_tokens: int | None
    observed_at: str


class AutomationStore:
    def __init__(self, db_file: Path) -> None:
        self._db_file = Path(db_file)

    def initialize(self) -> None:
        def initialize_schema(conn: sqlite3.Connection) -> None:
            self._ensure_schema(conn)

        self._run(initialize_schema)

    def reconcile_aliases(self, aliases: list[str]) -> None:
        normalized_aliases = sorted(set(aliases))

        def write_aliases(conn: sqlite3.Connection) -> None:
            if normalized_aliases:
                placeholders = ",".join("?" for _ in normalized_aliases)
                conn.execute(
                    f"DELETE FROM aliases WHERE alias NOT IN ({placeholders})",
                    tuple(normalized_aliases),
                )
            else:
                conn.execute("DELETE FROM aliases")

            conn.executemany(
                """
                INSERT INTO aliases (alias)
                VALUES (?)
                ON CONFLICT(alias) DO NOTHING
                """,
                [(alias,) for alias in normalized_aliases],
            )

        self._run(write_aliases)

    def record_alias_observation(
        self,
        *,
        alias: str,
        account_email: str | None,
        account_plan_type: str | None,
        account_fingerprint: str | None,
        observed_at: str,
    ) -> None:
        def write_alias_observation(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO aliases (
                    alias,
                    account_email,
                    account_plan_type,
                    account_fingerprint,
                    last_observed_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(alias) DO UPDATE SET
                    account_email = excluded.account_email,
                    account_plan_type = excluded.account_plan_type,
                    account_fingerprint = excluded.account_fingerprint,
                    last_observed_at = excluded.last_observed_at
                """,
                (
                    alias,
                    account_email,
                    account_plan_type,
                    account_fingerprint,
                    observed_at,
                ),
            )

        self._run(write_alias_observation)

    def list_aliases(self) -> list[AliasRecord]:
        def read_aliases(conn: sqlite3.Connection) -> list[AliasRecord]:
            rows = conn.execute(
                """
                SELECT alias, account_email, account_plan_type, account_fingerprint, last_observed_at
                FROM aliases
                ORDER BY alias ASC
                """
            ).fetchall()
            return [_row_to_alias_record(row) for row in rows]

        return self._run(read_aliases)

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

    def latest_rate_limit_for_alias(self, alias: str) -> RateLimitRecord | None:
        def read_latest(conn: sqlite3.Connection) -> RateLimitRecord | None:
            row = conn.execute(
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
                LIMIT 1
                """,
                (alias,),
            ).fetchone()
            if row is None:
                return None
            return _row_to_rate_limit_record(row)

        return self._run(read_latest)

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

    def get_handoff_state(self) -> HandoffStateRecord | None:
        def read_handoff_state(conn: sqlite3.Connection) -> HandoffStateRecord | None:
            row = conn.execute(
                """
                SELECT
                    thread_id,
                    source_alias,
                    target_alias,
                    phase,
                    reason,
                    updated_at
                FROM handoff_state
                WHERE singleton_key = ?
                """,
                (_HANDOFF_STATE_KEY,),
            ).fetchone()
            if row is None:
                return None
            return HandoffStateRecord(
                thread_id=row["thread_id"],
                source_alias=row["source_alias"],
                target_alias=row["target_alias"],
                phase=HandoffPhase(row["phase"]),
                reason=row["reason"],
                updated_at=row["updated_at"],
            )

        return self._run(read_handoff_state)

    def clear_handoff_state(self) -> None:
        def delete_handoff_state(conn: sqlite3.Connection) -> None:
            conn.execute(
                "DELETE FROM handoff_state WHERE singleton_key = ?",
                (_HANDOFF_STATE_KEY,),
            )

        self._run(delete_handoff_state)

    def append_switch_event(
        self,
        *,
        thread_id: str | None,
        from_alias: str | None,
        to_alias: str | None,
        trigger_type: str | None,
        trigger_limit_id: str | None,
        trigger_used_percent: float | None,
        requested_at: str,
        switched_at: str | None,
        resumed_at: str | None,
        result: str,
        failure_message: str | None,
    ) -> int:
        def write_switch_event(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                """
                INSERT INTO switch_events (
                    thread_id,
                    from_alias,
                    to_alias,
                    trigger_type,
                    trigger_limit_id,
                    trigger_used_percent,
                    requested_at,
                    switched_at,
                    resumed_at,
                    result,
                    failure_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread_id,
                    from_alias,
                    to_alias,
                    trigger_type,
                    trigger_limit_id,
                    trigger_used_percent,
                    requested_at,
                    switched_at,
                    resumed_at,
                    result,
                    failure_message,
                ),
            )
            return int(cursor.lastrowid)

        return self._run(write_switch_event)

    def list_switch_events(self, limit: int = 20) -> list[SwitchEventRecord]:
        if limit <= 0:
            return []

        def read_switch_events(conn: sqlite3.Connection) -> list[SwitchEventRecord]:
            rows = conn.execute(
                """
                SELECT
                    id,
                    thread_id,
                    from_alias,
                    to_alias,
                    trigger_type,
                    trigger_limit_id,
                    trigger_used_percent,
                    requested_at,
                    switched_at,
                    resumed_at,
                    result,
                    failure_message
                FROM switch_events
                ORDER BY requested_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [_row_to_switch_event_record(row) for row in rows]

        return self._run(read_switch_events)

    def upsert_thread_runtime(
        self,
        *,
        thread_id: str,
        cwd: str | None,
        model: str | None,
        current_alias: str | None,
        last_turn_id: str | None,
        last_known_status: str | None,
        safe_to_switch: bool,
        last_total_tokens: int | None,
        last_seen_at: str,
    ) -> None:
        def write_thread_runtime(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO thread_runtime (
                    thread_id,
                    cwd,
                    model,
                    current_alias,
                    last_turn_id,
                    last_known_status,
                    safe_to_switch,
                    last_total_tokens,
                    last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    cwd = excluded.cwd,
                    model = excluded.model,
                    current_alias = excluded.current_alias,
                    last_turn_id = excluded.last_turn_id,
                    last_known_status = excluded.last_known_status,
                    safe_to_switch = excluded.safe_to_switch,
                    last_total_tokens = excluded.last_total_tokens,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    thread_id,
                    cwd,
                    model,
                    current_alias,
                    last_turn_id,
                    last_known_status,
                    _bool_to_int(safe_to_switch),
                    last_total_tokens,
                    last_seen_at,
                ),
            )

        self._run(write_thread_runtime)

    def get_thread_runtime(self, thread_id: str) -> ThreadRuntimeRecord | None:
        def read_thread_runtime(conn: sqlite3.Connection) -> ThreadRuntimeRecord | None:
            row = conn.execute(
                """
                SELECT
                    thread_id,
                    cwd,
                    model,
                    current_alias,
                    last_turn_id,
                    last_known_status,
                    safe_to_switch,
                    last_total_tokens,
                    last_seen_at
                FROM thread_runtime
                WHERE thread_id = ?
                """,
                (thread_id,),
            ).fetchone()
            if row is None:
                return None
            return _row_to_thread_runtime_record(row)

        return self._run(read_thread_runtime)

    def append_thread_turn_usage(
        self,
        *,
        thread_id: str,
        turn_id: str,
        last_input_tokens: int | None,
        last_cached_input_tokens: int | None,
        last_output_tokens: int | None,
        last_reasoning_output_tokens: int | None,
        last_total_tokens: int | None,
        total_input_tokens: int | None,
        total_cached_input_tokens: int | None,
        total_output_tokens: int | None,
        total_reasoning_output_tokens: int | None,
        total_tokens: int | None,
        observed_at: str,
    ) -> int:
        def write_thread_turn_usage(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                """
                INSERT INTO thread_turn_usage (
                    thread_id,
                    turn_id,
                    last_input_tokens,
                    last_cached_input_tokens,
                    last_output_tokens,
                    last_reasoning_output_tokens,
                    last_total_tokens,
                    total_input_tokens,
                    total_cached_input_tokens,
                    total_output_tokens,
                    total_reasoning_output_tokens,
                    total_tokens,
                    observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread_id,
                    turn_id,
                    last_input_tokens,
                    last_cached_input_tokens,
                    last_output_tokens,
                    last_reasoning_output_tokens,
                    last_total_tokens,
                    total_input_tokens,
                    total_cached_input_tokens,
                    total_output_tokens,
                    total_reasoning_output_tokens,
                    total_tokens,
                    observed_at,
                ),
            )
            return int(cursor.lastrowid)

        return self._run(write_thread_turn_usage)

    def list_thread_turn_usage(self, *, thread_id: str, limit: int = 20) -> list[ThreadTurnUsageRecord]:
        if limit <= 0:
            return []

        def read_thread_turn_usage(conn: sqlite3.Connection) -> list[ThreadTurnUsageRecord]:
            rows = conn.execute(
                """
                SELECT
                    id,
                    thread_id,
                    turn_id,
                    last_input_tokens,
                    last_cached_input_tokens,
                    last_output_tokens,
                    last_reasoning_output_tokens,
                    last_total_tokens,
                    total_input_tokens,
                    total_cached_input_tokens,
                    total_output_tokens,
                    total_reasoning_output_tokens,
                    total_tokens,
                    observed_at
                FROM thread_turn_usage
                WHERE thread_id = ?
                ORDER BY observed_at DESC, id DESC
                LIMIT ?
                """,
                (thread_id, limit),
            ).fetchall()
            return [_row_to_thread_turn_usage_record(row) for row in rows]

        return self._run(read_thread_turn_usage)

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
        conn.executescript(_ALIASES_CREATE_SQL)
        conn.executescript(_RATE_LIMITS_CREATE_SQL)
        conn.executescript(_SWITCH_EVENTS_CREATE_SQL)
        conn.executescript(_THREAD_RUNTIME_CREATE_SQL)
        conn.executescript(_THREAD_TURN_USAGE_CREATE_SQL)
        self._ensure_rate_limits_index(conn)
        self._ensure_switch_events_index(conn)
        self._ensure_thread_runtime_index(conn)
        self._ensure_thread_turn_usage_index(conn)

        version = self._read_schema_version(conn)
        if version is None:
            self._set_schema_version(conn, SCHEMA_VERSION)
            return
        if version > SCHEMA_VERSION:
            raise AutomationDatabaseError(
                f"Unsupported automation schema version {version}; expected {SCHEMA_VERSION}"
            )

        if version < SCHEMA_VERSION or self._rate_limits_credits_balance_declared_type(conn) != "TEXT":
            self._migrate_rate_limits_credits_balance_to_text(conn)

        self._set_schema_version(conn, SCHEMA_VERSION)

    def _read_schema_version(self, conn: sqlite3.Connection) -> int | None:
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = ?",
            ("schema_version",),
        ).fetchone()
        if row is None:
            return None
        try:
            return int(row["value"])
        except (TypeError, ValueError) as exc:
            raise AutomationDatabaseError(
                f"Invalid automation schema version {row['value']!r}"
            ) from exc

    def _set_schema_version(self, conn: sqlite3.Connection, version: int) -> None:
        conn.execute(
            """
            INSERT INTO metadata (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            ("schema_version", str(version)),
        )

    def _rate_limits_credits_balance_declared_type(self, conn: sqlite3.Connection) -> str | None:
        row = conn.execute("PRAGMA table_info(rate_limits)").fetchall()
        for column in row:
            if column[1] == "credits_balance":
                return column[2]
        return None

    def _ensure_rate_limits_index(self, conn: sqlite3.Connection) -> None:
        conn.execute(_RATE_LIMITS_INDEX_SQL)

    def _ensure_switch_events_index(self, conn: sqlite3.Connection) -> None:
        conn.execute(_SWITCH_EVENTS_INDEX_SQL)

    def _ensure_thread_runtime_index(self, conn: sqlite3.Connection) -> None:
        conn.execute(_THREAD_RUNTIME_INDEX_SQL)

    def _ensure_thread_turn_usage_index(self, conn: sqlite3.Connection) -> None:
        conn.execute(_THREAD_TURN_USAGE_INDEX_SQL)

    def _migrate_rate_limits_credits_balance_to_text(self, conn: sqlite3.Connection) -> None:
        declared_type = self._rate_limits_credits_balance_declared_type(conn)
        if declared_type is None:
            conn.executescript(_RATE_LIMITS_CREATE_SQL)
            self._ensure_rate_limits_index(conn)
            return
        if declared_type == "TEXT":
            self._ensure_rate_limits_index(conn)
            return

        conn.execute("DROP INDEX IF EXISTS idx_rate_limits_alias")
        conn.execute("ALTER TABLE rate_limits RENAME TO rate_limits_legacy")
        conn.executescript(_RATE_LIMITS_CREATE_SQL)
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
            )
            SELECT
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
                CAST(credits_balance AS TEXT),
                observed_at
            FROM rate_limits_legacy
            """
        )
        conn.execute("DROP TABLE rate_limits_legacy")
        self._ensure_rate_limits_index(conn)


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
        credits_balance=None if row["credits_balance"] is None else str(row["credits_balance"]),
        observed_at=row["observed_at"],
    )


def _row_to_alias_record(row: sqlite3.Row) -> AliasRecord:
    return AliasRecord(
        alias=row["alias"],
        account_email=row["account_email"],
        account_plan_type=row["account_plan_type"],
        account_fingerprint=row["account_fingerprint"],
        last_observed_at=row["last_observed_at"],
    )


def _row_to_thread_runtime_record(row: sqlite3.Row) -> ThreadRuntimeRecord:
    return ThreadRuntimeRecord(
        thread_id=row["thread_id"],
        cwd=row["cwd"],
        model=row["model"],
        current_alias=row["current_alias"],
        last_turn_id=row["last_turn_id"],
        last_known_status=row["last_known_status"],
        safe_to_switch=bool(row["safe_to_switch"]),
        last_total_tokens=row["last_total_tokens"],
        last_seen_at=row["last_seen_at"],
    )


def _row_to_thread_turn_usage_record(row: sqlite3.Row) -> ThreadTurnUsageRecord:
    return ThreadTurnUsageRecord(
        id=row["id"],
        thread_id=row["thread_id"],
        turn_id=row["turn_id"],
        last_input_tokens=row["last_input_tokens"],
        last_cached_input_tokens=row["last_cached_input_tokens"],
        last_output_tokens=row["last_output_tokens"],
        last_reasoning_output_tokens=row["last_reasoning_output_tokens"],
        last_total_tokens=row["last_total_tokens"],
        total_input_tokens=row["total_input_tokens"],
        total_cached_input_tokens=row["total_cached_input_tokens"],
        total_output_tokens=row["total_output_tokens"],
        total_reasoning_output_tokens=row["total_reasoning_output_tokens"],
        total_tokens=row["total_tokens"],
        observed_at=row["observed_at"],
    )


def _row_to_switch_event_record(row: sqlite3.Row) -> SwitchEventRecord:
    return SwitchEventRecord(
        id=row["id"],
        thread_id=row["thread_id"],
        from_alias=row["from_alias"],
        to_alias=row["to_alias"],
        trigger_type=row["trigger_type"],
        trigger_limit_id=row["trigger_limit_id"],
        trigger_used_percent=row["trigger_used_percent"],
        requested_at=row["requested_at"],
        switched_at=row["switched_at"],
        resumed_at=row["resumed_at"],
        result=row["result"],
        failure_message=row["failure_message"],
    )
