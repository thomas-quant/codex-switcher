from __future__ import annotations

import sqlite3

import pytest

from codex_switch.automation_db import AutomationStore
from codex_switch.automation_models import HandoffPhase, RateLimitSnapshot, RateLimitWindow, UsageSource
from codex_switch.errors import AutomationDatabaseError
from codex_switch.paths import resolve_paths


def test_store_creates_schema_and_records_rate_limit(tmp_path):
    paths = resolve_paths(tmp_path)
    store = AutomationStore(paths.automation_db_file)

    store.initialize()
    store.upsert_rate_limit(
        RateLimitSnapshot(
            alias="work",
            limit_id=None,
            limit_name="Daily limit",
            observed_via=UsageSource.RPC,
            plan_type="pro",
            primary_window=RateLimitWindow(
                used_percent=42,
                resets_at="2026-04-04T00:00:00Z",
                window_duration_mins=60,
            ),
            secondary_window=RateLimitWindow(
                used_percent=None,
                resets_at=None,
                window_duration_mins=None,
            ),
            credits_has_credits=True,
            credits_unlimited=False,
            credits_balance="5.25",
            observed_at="2026-04-04T00:00:00Z",
        )
    )

    rows = store.list_rate_limits_for_alias("work")

    assert len(rows) == 1
    assert rows[0].alias == "work"
    assert rows[0].limit_id is None
    assert rows[0].primary_used_percent == 42
    assert rows[0].credits_balance == "5.25"
    assert oct(paths.switch_root.stat().st_mode & 0o777) == "0o700"
    assert oct(paths.automation_db_file.stat().st_mode & 0o777) == "0o600"

    with sqlite3.connect(paths.automation_db_file) as conn:
        column_types = {
            row[1]: row[2]
            for row in conn.execute("PRAGMA table_info(rate_limits)").fetchall()
        }

    assert column_types["credits_balance"] == "TEXT"


def test_store_updates_rate_limit_in_place_for_same_key(tmp_path):
    paths = resolve_paths(tmp_path)
    store = AutomationStore(paths.automation_db_file)

    store.initialize()
    store.upsert_rate_limit(
        RateLimitSnapshot(
            alias="work",
            limit_id=None,
            limit_name="Daily limit",
            observed_via=UsageSource.RPC,
            plan_type="pro",
            primary_window=RateLimitWindow(
                used_percent=10,
                resets_at="2026-04-04T00:00:00Z",
                window_duration_mins=60,
            ),
            secondary_window=RateLimitWindow(
                used_percent=None,
                resets_at=None,
                window_duration_mins=None,
            ),
            credits_has_credits=True,
            credits_unlimited=False,
            credits_balance="5.25",
            observed_at="2026-04-04T00:00:00Z",
        )
    )
    store.upsert_rate_limit(
        RateLimitSnapshot(
            alias="work",
            limit_id=None,
            limit_name="Daily limit",
            observed_via=UsageSource.PTY,
            plan_type="team",
            primary_window=RateLimitWindow(
                used_percent=42,
                resets_at="2026-04-04T01:00:00Z",
                window_duration_mins=120,
            ),
            secondary_window=RateLimitWindow(
                used_percent=7,
                resets_at="2026-04-04T02:00:00Z",
                window_duration_mins=240,
            ),
            credits_has_credits=False,
            credits_unlimited=True,
            credits_balance="1.00",
            observed_at="2026-04-04T01:00:00Z",
        )
    )

    rows = store.list_rate_limits_for_alias("work")

    assert len(rows) == 1
    assert rows[0].observed_via == UsageSource.PTY
    assert rows[0].primary_used_percent == 42
    assert rows[0].secondary_used_percent == 7
    assert rows[0].credits_balance == "1.00"


def test_initialize_migrates_rate_limit_credits_balance_to_text(tmp_path):
    paths = resolve_paths(tmp_path)
    paths.switch_root.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(paths.automation_db_file) as conn:
        conn.executescript(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO metadata (key, value) VALUES ('schema_version', '1');

            CREATE TABLE rate_limits (
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
            ) VALUES (
                'work',
                NULL,
                'null',
                'Daily limit',
                'PTY',
                'pro',
                42,
                '2026-04-04T00:00:00Z',
                60,
                91,
                '2026-04-05T00:00:00Z',
                10080,
                1,
                0,
                5.25,
                '2026-04-04T00:00:00Z'
            );
            """
        )

    store = AutomationStore(paths.automation_db_file)
    store.initialize()
    store.upsert_rate_limit(
        RateLimitSnapshot(
            alias="work",
            limit_id=None,
            limit_name="Daily limit",
            observed_via=UsageSource.PTY,
            plan_type="pro",
            primary_window=RateLimitWindow(
                used_percent=43,
                resets_at="2026-04-04T01:00:00Z",
                window_duration_mins=60,
            ),
            secondary_window=RateLimitWindow(
                used_percent=92,
                resets_at="2026-04-05T01:00:00Z",
                window_duration_mins=10080,
            ),
            credits_has_credits=True,
            credits_unlimited=False,
            credits_balance="7.50",
            observed_at="2026-04-04T01:00:00Z",
        )
    )

    rows = store.list_rate_limits_for_alias("work")
    assert len(rows) == 1
    assert rows[0].credits_balance == "7.50"

    with sqlite3.connect(paths.automation_db_file) as conn:
        column_types = {
            row[1]: row[2]
            for row in conn.execute("PRAGMA table_info(rate_limits)").fetchall()
        }
        stored_credit = conn.execute(
            "SELECT credits_balance, typeof(credits_balance) FROM rate_limits WHERE alias = ?",
            ("work",),
        ).fetchone()
        schema_version = conn.execute(
            "SELECT value FROM metadata WHERE key = ?",
            ("schema_version",),
        ).fetchone()[0]

    assert column_types["credits_balance"] == "TEXT"
    assert stored_credit == ("7.50", "text")
    assert schema_version == "3"


def test_store_persists_handoff_state(tmp_path):
    paths = resolve_paths(tmp_path)
    store = AutomationStore(paths.automation_db_file)

    store.initialize()
    store.set_handoff_state(
        thread_id="thread-1",
        source_alias="work",
        target_alias="personal",
        phase=HandoffPhase.pending_switch,
        reason="switching accounts",
        updated_at="2026-04-04T12:00:00Z",
    )
    store.set_handoff_state(
        thread_id="thread-2",
        source_alias="personal",
        target_alias="work",
        phase=HandoffPhase.pending_resume,
        reason="resuming after switch",
        updated_at="2026-04-04T12:05:00Z",
    )

    with sqlite3.connect(paths.automation_db_file) as conn:
        rows = conn.execute(
            """
            SELECT thread_id, source_alias, target_alias, phase, reason, updated_at
            FROM handoff_state
            ORDER BY singleton_key
            """
        ).fetchall()

    assert rows == [
        (
            "thread-2",
            "personal",
            "work",
            HandoffPhase.pending_resume.value,
            "resuming after switch",
            "2026-04-04T12:05:00Z",
        )
    ]


def test_initialize_rejects_symlinked_db_path(tmp_path):
    paths = resolve_paths(tmp_path)
    store = AutomationStore(paths.automation_db_file)
    outside_target = tmp_path / "outside" / "automation.sqlite"
    paths.switch_root.mkdir(parents=True, exist_ok=True)
    paths.automation_db_file.symlink_to(outside_target)

    with pytest.raises(AutomationDatabaseError, match="symlink"):
        store.initialize()

    assert not outside_target.exists()


def test_store_reads_latest_rate_limit_for_alias(tmp_path):
    paths = resolve_paths(tmp_path)
    store = AutomationStore(paths.automation_db_file)
    store.initialize()
    store.upsert_rate_limit(
        RateLimitSnapshot(
            alias="work",
            limit_id=None,
            limit_name="Daily limit",
            observed_via=UsageSource.PTY,
            plan_type="pro",
            primary_window=RateLimitWindow(
                used_percent=80,
                resets_at="2026-04-04T00:00:00Z",
                window_duration_mins=60,
            ),
            secondary_window=RateLimitWindow(
                used_percent=60,
                resets_at="2026-04-05T00:00:00Z",
                window_duration_mins=10080,
            ),
            credits_has_credits=True,
            credits_unlimited=False,
            credits_balance="3.5",
            observed_at="2026-04-04T00:00:00Z",
        )
    )
    store.upsert_rate_limit(
        RateLimitSnapshot(
            alias="work",
            limit_id="weekly",
            limit_name="Weekly limit",
            observed_via=UsageSource.RPC,
            plan_type="pro",
            primary_window=RateLimitWindow(
                used_percent=30,
                resets_at="2026-04-06T00:00:00Z",
                window_duration_mins=300,
            ),
            secondary_window=RateLimitWindow(
                used_percent=20,
                resets_at="2026-04-07T00:00:00Z",
                window_duration_mins=10080,
            ),
            credits_has_credits=True,
            credits_unlimited=False,
            credits_balance="2.0",
            observed_at="2026-04-04T01:00:00Z",
        )
    )

    latest = store.latest_rate_limit_for_alias("work")

    assert latest is not None
    assert latest.limit_id == "weekly"
    assert latest.observed_via == UsageSource.RPC
    assert store.latest_rate_limit_for_alias("missing") is None


def test_store_gets_and_clears_handoff_state(tmp_path):
    paths = resolve_paths(tmp_path)
    store = AutomationStore(paths.automation_db_file)
    store.initialize()
    store.set_handoff_state(
        thread_id="thread-1",
        source_alias="a",
        target_alias="b",
        phase=HandoffPhase.failed_resume,
        reason="resume failed",
        updated_at="2026-04-05T01:00:00Z",
    )

    record = store.get_handoff_state()
    assert record is not None
    assert record.phase == HandoffPhase.failed_resume
    assert record.thread_id == "thread-1"

    store.clear_handoff_state()
    assert store.get_handoff_state() is None


def test_store_records_and_lists_switch_events(tmp_path):
    paths = resolve_paths(tmp_path)
    store = AutomationStore(paths.automation_db_file)
    store.initialize()

    first_id = store.append_switch_event(
        thread_id="thread-1",
        from_alias="work",
        to_alias="backup",
        trigger_type="soft",
        trigger_limit_id=None,
        trigger_used_percent=95.0,
        requested_at="2026-04-05T01:00:00Z",
        switched_at="2026-04-05T01:00:05Z",
        resumed_at="2026-04-05T01:00:10Z",
        result="success",
        failure_message=None,
    )
    second_id = store.append_switch_event(
        thread_id="thread-2",
        from_alias="backup",
        to_alias="work",
        trigger_type="hard",
        trigger_limit_id="weekly",
        trigger_used_percent=100.0,
        requested_at="2026-04-05T02:00:00Z",
        switched_at=None,
        resumed_at=None,
        result="failed_resume",
        failure_message="resume failed",
    )

    rows = store.list_switch_events(limit=10)
    assert [row.id for row in rows] == [second_id, first_id]
    assert rows[0].result == "failed_resume"
    assert rows[1].result == "success"
    assert store.list_switch_events(limit=0) == []


def test_store_reconciles_alias_inventory_and_updates_observation_metadata(tmp_path):
    paths = resolve_paths(tmp_path)
    store = AutomationStore(paths.automation_db_file)
    store.initialize()

    store.reconcile_aliases(["work", "backup"])
    store.record_alias_observation(
        alias="work",
        account_email="work@example.com",
        account_plan_type="pro",
        account_fingerprint="fp-work",
        observed_at="2026-04-05T00:00:00Z",
    )
    store.reconcile_aliases(["backup", "personal"])

    rows = store.list_aliases()

    assert [row.alias for row in rows] == ["backup", "personal"]
    assert rows[0].account_email is None
    assert rows[1].account_email is None


def test_store_upserts_thread_runtime_rows(tmp_path):
    paths = resolve_paths(tmp_path)
    store = AutomationStore(paths.automation_db_file)
    store.initialize()

    store.upsert_thread_runtime(
        thread_id="thread-1",
        cwd="/repo",
        model="gpt-5.4",
        current_alias="work",
        last_turn_id="turn-1",
        last_known_status="running",
        safe_to_switch=False,
        last_total_tokens=100,
        last_seen_at="2026-04-05T00:00:00Z",
    )
    store.upsert_thread_runtime(
        thread_id="thread-1",
        cwd="/repo",
        model="gpt-5.4",
        current_alias="work",
        last_turn_id="turn-2",
        last_known_status="idle",
        safe_to_switch=True,
        last_total_tokens=150,
        last_seen_at="2026-04-05T00:01:00Z",
    )

    latest = store.get_thread_runtime("thread-1")

    assert latest is not None
    assert latest.last_turn_id == "turn-2"
    assert latest.last_known_status == "idle"
    assert latest.safe_to_switch is True
    assert latest.last_total_tokens == 150


def test_store_appends_thread_turn_usage_history(tmp_path):
    paths = resolve_paths(tmp_path)
    store = AutomationStore(paths.automation_db_file)
    store.initialize()

    store.append_thread_turn_usage(
        thread_id="thread-1",
        turn_id="turn-1",
        last_input_tokens=10,
        last_cached_input_tokens=2,
        last_output_tokens=5,
        last_reasoning_output_tokens=1,
        last_total_tokens=18,
        total_input_tokens=10,
        total_cached_input_tokens=2,
        total_output_tokens=5,
        total_reasoning_output_tokens=1,
        total_tokens=18,
        observed_at="2026-04-05T00:00:00Z",
    )
    store.append_thread_turn_usage(
        thread_id="thread-1",
        turn_id="turn-2",
        last_input_tokens=3,
        last_cached_input_tokens=0,
        last_output_tokens=8,
        last_reasoning_output_tokens=2,
        last_total_tokens=13,
        total_input_tokens=13,
        total_cached_input_tokens=2,
        total_output_tokens=13,
        total_reasoning_output_tokens=3,
        total_tokens=31,
        observed_at="2026-04-05T00:01:00Z",
    )

    rows = store.list_thread_turn_usage(thread_id="thread-1", limit=10)

    assert [row.turn_id for row in rows] == ["turn-2", "turn-1"]
    assert rows[0].total_tokens == 31
