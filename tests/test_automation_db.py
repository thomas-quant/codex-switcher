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
            credits_balance=5,
            observed_at="2026-04-04T00:00:00Z",
        )
    )

    rows = store.list_rate_limits_for_alias("work")

    assert len(rows) == 1
    assert rows[0].alias == "work"
    assert rows[0].limit_id is None
    assert rows[0].primary_used_percent == 42
    assert oct(paths.switch_root.stat().st_mode & 0o777) == "0o700"
    assert oct(paths.automation_db_file.stat().st_mode & 0o777) == "0o600"


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
            credits_balance=5,
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
            credits_balance=1,
            observed_at="2026-04-04T01:00:00Z",
        )
    )

    rows = store.list_rate_limits_for_alias("work")

    assert len(rows) == 1
    assert rows[0].observed_via == UsageSource.PTY
    assert rows[0].primary_used_percent == 42
    assert rows[0].secondary_used_percent == 7


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
