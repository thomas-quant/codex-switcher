from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading

import pytest

from codex_switch.accounts import AccountStore
from codex_switch.automation_db import AutomationStore
from codex_switch.automation_models import RateLimitSnapshot, RateLimitWindow, UsageSource
from codex_switch.manager import CodexSwitchManager
from codex_switch.models import AliasListEntry, AliasTelemetryObservation, AppState
from codex_switch.paths import resolve_paths
from codex_switch.state import StateStore


class MutationGuardSpy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def make_manager(tmp_path, *, alias_metadata_probe=None, initialize_automation: bool = True):
    paths = resolve_paths(tmp_path)
    accounts = AccountStore(paths.accounts_dir)
    state = StateStore(paths.state_file)
    store = AutomationStore(paths.automation_db_file)
    if initialize_automation:
        store.initialize()
    guard = MutationGuardSpy()
    manager = CodexSwitchManager(
        paths=paths,
        accounts=accounts,
        state=state,
        ensure_safe_to_mutate=guard,
        login_runner=lambda _mode: None,
        automation=store,
        alias_metadata_probe=alias_metadata_probe,
    )
    return manager, paths, accounts, state, store, guard


def make_observed_at(*, minutes_ago: int) -> str:
    return (
        (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def test_list_aliases_uses_cached_plan_types(tmp_path):
    manager, _paths, accounts, state, store, _guard = make_manager(tmp_path)
    accounts.write_snapshot_from_bytes("backup", b"{}")
    accounts.write_snapshot_from_bytes("beta", b"{}")
    store.reconcile_aliases(["backup", "beta"])
    store.record_alias_observation(
        alias="beta",
        account_email="beta@example.com",
        account_plan_type="plus",
        account_fingerprint="fp-beta",
        observed_at="2026-04-05T00:00:00Z",
    )
    state.save(AppState(active_alias="beta", updated_at="2026-04-05T00:00:00Z"))

    entries, active_alias = manager.list_aliases()

    assert entries == [
        AliasListEntry(
            alias="backup",
            plan_type=None,
            five_hour_left_percent=None,
            weekly_left_percent=None,
        ),
        AliasListEntry(
            alias="beta",
            plan_type="plus",
            five_hour_left_percent=None,
            weekly_left_percent=None,
        ),
    ]
    assert active_alias == "beta"


def test_list_aliases_includes_cached_account_email(tmp_path):
    manager, _paths, accounts, state, store, _guard = make_manager(tmp_path)
    accounts.write_snapshot_from_bytes("beta", b"{}")
    store.reconcile_aliases(["beta"])
    store.record_alias_observation(
        alias="beta",
        account_email="beta@example.com",
        account_plan_type="plus",
        account_fingerprint="fp-beta",
        observed_at="2026-04-05T00:00:00Z",
    )
    state.save(AppState(active_alias="beta", updated_at="2026-04-05T00:00:00Z"))

    entries, active_alias = manager.list_aliases(refresh=False, include_email=True)

    assert entries == [
        AliasListEntry(
            alias="beta",
            plan_type="plus",
            account_email="beta@example.com",
            five_hour_left_percent=None,
            weekly_left_percent=None,
        )
    ]
    assert active_alias == "beta"


def test_list_aliases_does_not_create_automation_db_for_cold_cache(tmp_path):
    manager, _paths, accounts, state, store, _guard = make_manager(
        tmp_path,
        initialize_automation=False,
    )
    accounts.write_snapshot_from_bytes("backup", b"{}")
    accounts.write_snapshot_from_bytes("beta", b"{}")
    state.save(AppState(active_alias="beta", updated_at="2026-04-05T00:00:00Z"))

    assert not store._db_file.exists()

    entries, active_alias = manager.list_aliases()

    assert entries == [
        AliasListEntry(
            alias="backup",
            plan_type=None,
            five_hour_left_percent=None,
            weekly_left_percent=None,
        ),
        AliasListEntry(
            alias="beta",
            plan_type=None,
            five_hour_left_percent=None,
            weekly_left_percent=None,
        ),
    ]
    assert active_alias == "beta"
    assert not store._db_file.exists()


def test_list_aliases_uses_cache_only_when_refresh_is_disabled(tmp_path):
    probe_calls: list[str] = []

    def alias_metadata_probe(alias: str) -> AliasTelemetryObservation | None:
        probe_calls.append(alias)
        return AliasTelemetryObservation(
            account_email=f"{alias}@example.com",
            account_plan_type="plus",
            account_fingerprint=f"fp-{alias}",
            observed_at="2026-04-05T00:10:00Z",
        )

    manager, _paths, accounts, state, store, _guard = make_manager(
        tmp_path,
        alias_metadata_probe=alias_metadata_probe,
    )
    accounts.write_snapshot_from_bytes("beta", b"{}")
    store.reconcile_aliases(["beta"])
    state.save(AppState(active_alias="beta", updated_at="2026-04-05T00:00:00Z"))

    entries, active_alias = manager.list_aliases(refresh=False)

    assert entries == [
        AliasListEntry(
            alias="beta",
            plan_type=None,
            five_hour_left_percent=None,
            weekly_left_percent=None,
        )
    ]
    assert active_alias == "beta"
    assert probe_calls == []


def test_list_aliases_persists_probe_results_for_cold_cache(tmp_path):
    def alias_metadata_probe(alias: str) -> AliasTelemetryObservation | None:
        return AliasTelemetryObservation(
            account_email=f"{alias}@example.com",
            account_plan_type="plus",
            account_fingerprint=f"fp-{alias}",
            observed_at="2026-04-05T00:10:00Z",
        )

    manager, _paths, accounts, state, store, _guard = make_manager(
        tmp_path,
        alias_metadata_probe=alias_metadata_probe,
        initialize_automation=False,
    )
    accounts.write_snapshot_from_bytes("beta", b"{}")
    state.save(AppState(active_alias="beta", updated_at="2026-04-05T00:00:00Z"))

    assert not store._db_file.exists()

    entries, active_alias = manager.list_aliases()

    assert entries == [
        AliasListEntry(
            alias="beta",
            plan_type="plus",
            five_hour_left_percent=None,
            weekly_left_percent=None,
        )
    ]
    assert active_alias == "beta"
    assert store._db_file.exists()
    assert store.list_aliases()[0].account_plan_type == "plus"


def test_list_aliases_falls_back_when_automation_db_is_unavailable(tmp_path):
    manager, _paths, accounts, state, store, _guard = make_manager(tmp_path)
    accounts.write_snapshot_from_bytes("backup", b"{}")
    accounts.write_snapshot_from_bytes("beta", b"{}")
    state.save(AppState(active_alias="beta", updated_at="2026-04-05T00:00:00Z"))

    target = tmp_path / "automation-target.sqlite"
    target.write_bytes(b"")
    db_file = store._db_file
    db_file.unlink()
    db_file.symlink_to(target)

    entries, active_alias = manager.list_aliases()

    assert entries == [
        AliasListEntry(
            alias="backup",
            plan_type=None,
            five_hour_left_percent=None,
            weekly_left_percent=None,
        ),
        AliasListEntry(
            alias="beta",
            plan_type=None,
            five_hour_left_percent=None,
            weekly_left_percent=None,
        ),
    ]
    assert active_alias == "beta"


def test_list_aliases_refreshes_missing_plan_type_for_active_alias(tmp_path):
    probe_calls: list[str] = []

    def alias_metadata_probe(alias: str) -> AliasTelemetryObservation | None:
        probe_calls.append(alias)
        return AliasTelemetryObservation(
            account_email="beta@example.com",
            account_plan_type="plus",
            account_fingerprint="fp-beta",
            observed_at="2026-04-05T00:10:00Z",
        )

    manager, _paths, accounts, state, store, guard = make_manager(
        tmp_path,
        alias_metadata_probe=alias_metadata_probe,
    )
    accounts.write_snapshot_from_bytes("beta", b"{}")
    store.reconcile_aliases(["beta"])
    state.save(AppState(active_alias="beta", updated_at="2026-04-05T00:00:00Z"))

    entries, active_alias = manager.list_aliases()

    assert entries == [
        AliasListEntry(
            alias="beta",
            plan_type="plus",
            five_hour_left_percent=None,
            weekly_left_percent=None,
        )
    ]
    assert active_alias == "beta"
    assert probe_calls == ["beta"]
    assert guard.calls == 0


def test_list_aliases_refreshes_missing_plan_type_for_inactive_alias_and_restores_auth(tmp_path):
    probe_calls: list[str] = []

    def alias_metadata_probe(alias: str) -> AliasTelemetryObservation | None:
        probe_calls.append(alias)
        if alias != "backup":
            return None
        return AliasTelemetryObservation(
            account_email="backup@example.com",
            account_plan_type="pro",
            account_fingerprint="fp-backup",
            observed_at="2026-04-05T00:15:00Z",
        )

    manager, paths, accounts, state, store, guard = make_manager(
        tmp_path,
        alias_metadata_probe=alias_metadata_probe,
    )
    accounts.write_snapshot_from_bytes("work", b'{"token":"snapshot-work"}')
    accounts.write_snapshot_from_bytes("backup", b'{"token":"snapshot-backup"}')
    store.reconcile_aliases(["backup", "work"])
    state.save(AppState(active_alias="work", updated_at="2026-04-05T00:00:00Z"))
    paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.live_auth_file.write_bytes(b'{"token":"live-work"}')

    entries, active_alias = manager.list_aliases()

    assert entries == [
        AliasListEntry(
            alias="backup",
            plan_type="pro",
            five_hour_left_percent=None,
            weekly_left_percent=None,
        ),
        AliasListEntry(
            alias="work",
            plan_type=None,
            five_hour_left_percent=None,
            weekly_left_percent=None,
        ),
    ]
    assert active_alias == "work"
    assert probe_calls == ["backup", "work"]
    assert guard.calls == 0
    assert paths.live_auth_file.read_bytes() == b'{"token":"live-work"}'
    assert state.load() == AppState(active_alias="work", updated_at="2026-04-05T00:00:00Z")


def test_list_aliases_preserves_dirty_active_snapshot_during_inactive_refresh(tmp_path):
    def alias_metadata_probe(alias: str) -> AliasTelemetryObservation | None:
        if alias != "backup":
            return None
        return AliasTelemetryObservation(
            account_email="backup@example.com",
            account_plan_type="pro",
            account_fingerprint="fp-backup",
            observed_at="2026-04-05T00:15:00Z",
        )

    manager, paths, accounts, state, store, _guard = make_manager(
        tmp_path,
        alias_metadata_probe=alias_metadata_probe,
    )
    accounts.write_snapshot_from_bytes("work", b'{"token":"snapshot-work"}')
    accounts.write_snapshot_from_bytes("backup", b'{"token":"snapshot-backup"}')
    store.reconcile_aliases(["backup", "work"])
    state.save(AppState(active_alias="work", updated_at="2026-04-05T00:00:00Z"))
    paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.live_auth_file.write_bytes(b'{"token":"live-work"}')

    manager.list_aliases()

    assert accounts.read_snapshot("work") == b'{"token":"snapshot-work"}'
    assert paths.live_auth_file.read_bytes() == b'{"token":"live-work"}'


def test_list_aliases_does_not_touch_live_auth_when_inactive_probe_succeeds(tmp_path, monkeypatch):
    def alias_metadata_probe(alias: str) -> AliasTelemetryObservation | None:
        if alias != "backup":
            return None
        return AliasTelemetryObservation(
            account_email="backup@example.com",
            account_plan_type="pro",
            account_fingerprint="fp-backup",
            observed_at="2026-04-05T00:15:00Z",
        )

    manager, paths, accounts, state, store, _guard = make_manager(
        tmp_path,
        alias_metadata_probe=alias_metadata_probe,
    )
    accounts.write_snapshot_from_bytes("work", b'{"token":"snapshot-work"}')
    accounts.write_snapshot_from_bytes("backup", b'{"token":"snapshot-backup"}')
    store.reconcile_aliases(["backup", "work"])
    state.save(AppState(active_alias="work", updated_at="2026-04-05T00:00:00Z"))
    paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.live_auth_file.write_bytes(b'{"token":"live-work"}')

    restore_calls = 0

    def failing_restore(
        _previous_state,
        _backup_path,
        _clear_unmanaged_live_auth,
        prefer_live_backup: bool = False,
    ):
        nonlocal restore_calls
        restore_calls += 1
        raise RuntimeError("restore failed")

    monkeypatch.setattr(manager, "_restore_previous_live_auth", failing_restore)

    entries, active_alias = manager.list_aliases()

    assert entries == [
        AliasListEntry(
            alias="backup",
            plan_type="pro",
            five_hour_left_percent=None,
            weekly_left_percent=None,
        ),
        AliasListEntry(
            alias="work",
            plan_type=None,
            five_hour_left_percent=None,
            weekly_left_percent=None,
        ),
    ]
    assert active_alias == "work"
    assert restore_calls == 0


def test_list_aliases_refreshes_inactive_plan_type_without_mutation_guard(tmp_path):
    probe_calls: list[str] = []

    def alias_metadata_probe(alias: str) -> AliasTelemetryObservation | None:
        probe_calls.append(alias)
        return AliasTelemetryObservation(
            account_email="backup@example.com",
            account_plan_type="pro",
            account_fingerprint="fp-backup",
            observed_at="2026-04-05T00:15:00Z",
        )

    manager, _paths, accounts, state, store, _guard = make_manager(
        tmp_path,
        alias_metadata_probe=alias_metadata_probe,
    )

    def failing_guard() -> None:
        raise RuntimeError("codex is running")

    manager._ensure_safe_to_mutate = failing_guard
    accounts.write_snapshot_from_bytes("backup", b"{}")
    store.reconcile_aliases(["backup"])
    state.save(AppState(active_alias=None, updated_at="2026-04-05T00:00:00Z"))

    entries, active_alias = manager.list_aliases()

    assert entries == [
        AliasListEntry(
            alias="backup",
            plan_type="pro",
            five_hour_left_percent=None,
            weekly_left_percent=None,
        )
    ]
    assert active_alias is None
    assert probe_calls == ["backup"]


def test_list_aliases_ignores_probe_failures_and_keeps_plain_alias(tmp_path):
    def alias_metadata_probe(_alias: str) -> AliasTelemetryObservation | None:
        raise RuntimeError("rpc unavailable")

    manager, _paths, accounts, state, store, _guard = make_manager(
        tmp_path,
        alias_metadata_probe=alias_metadata_probe,
    )
    accounts.write_snapshot_from_bytes("backup", b"{}")
    store.reconcile_aliases(["backup"])
    state.save(AppState(active_alias="backup", updated_at="2026-04-05T00:00:00Z"))

    entries, active_alias = manager.list_aliases()

    assert entries == [
        AliasListEntry(
            alias="backup",
            plan_type=None,
            five_hour_left_percent=None,
            weekly_left_percent=None,
        )
    ]
    assert active_alias == "backup"


def test_list_aliases_refreshes_unresolved_aliases_in_parallel(tmp_path):
    started: list[str] = []
    release = threading.Event()

    def alias_metadata_probe(alias: str) -> AliasTelemetryObservation | None:
        started.append(alias)
        if len(started) == 2:
            release.set()
        if not release.wait(timeout=0.2):
            return None
        return AliasTelemetryObservation(
            account_email=f"{alias}@example.com",
            account_plan_type="plus",
            account_fingerprint=f"fp-{alias}",
            observed_at="2026-04-13T10:00:00Z",
        )

    manager, _paths, accounts, state, store, _guard = make_manager(
        tmp_path,
        alias_metadata_probe=alias_metadata_probe,
    )
    accounts.write_snapshot_from_bytes("alpha", b"{}")
    accounts.write_snapshot_from_bytes("beta", b"{}")
    store.reconcile_aliases(["alpha", "beta"])
    state.save(AppState(active_alias=None, updated_at="2026-04-13T09:00:00Z"))

    entries, active_alias = manager.list_aliases()

    assert entries == [
        AliasListEntry(
            alias="alpha",
            plan_type="plus",
            five_hour_left_percent=None,
            weekly_left_percent=None,
        ),
        AliasListEntry(
            alias="beta",
            plan_type="plus",
            five_hour_left_percent=None,
            weekly_left_percent=None,
        ),
    ]
    assert active_alias is None
    assert sorted(started) == ["alpha", "beta"]


def test_list_aliases_refreshes_stale_usage_for_active_alias(tmp_path):
    probe_calls: list[str] = []
    stale_observed_at = make_observed_at(minutes_ago=30)
    fresh_observed_at = make_observed_at(minutes_ago=1)

    def alias_metadata_probe(alias: str) -> AliasTelemetryObservation | None:
        probe_calls.append(alias)
        return AliasTelemetryObservation(
            account_email="beta@example.com",
            account_plan_type="plus",
            account_fingerprint="fp-beta",
            observed_at=fresh_observed_at,
            rate_limits=(
                RateLimitSnapshot(
                    alias=alias,
                    limit_id="codex",
                    limit_name="codex",
                    observed_via=UsageSource.RPC,
                    plan_type="plus",
                    primary_window=RateLimitWindow(
                        used_percent=12,
                        resets_at="2026-04-06T05:00:00Z",
                        window_duration_mins=300,
                    ),
                    secondary_window=RateLimitWindow(
                        used_percent=88,
                        resets_at="2026-04-10T00:00:00Z",
                        window_duration_mins=10080,
                    ),
                    credits_has_credits=None,
                    credits_unlimited=None,
                    credits_balance=None,
                    observed_at=fresh_observed_at,
                ),
            ),
        )

    manager, _paths, accounts, state, store, _guard = make_manager(
        tmp_path,
        alias_metadata_probe=alias_metadata_probe,
    )
    accounts.write_snapshot_from_bytes("beta", b"{}")
    store.reconcile_aliases(["beta"])
    store.record_alias_observation(
        alias="beta",
        account_email="beta@example.com",
        account_plan_type="plus",
        account_fingerprint="fp-beta",
        observed_at=stale_observed_at,
    )
    store.upsert_rate_limit(
        RateLimitSnapshot(
            alias="beta",
            limit_id="codex",
            limit_name="codex",
            observed_via=UsageSource.RPC,
            plan_type="plus",
            primary_window=RateLimitWindow(
                used_percent=58,
                resets_at="2026-04-06T05:00:00Z",
                window_duration_mins=300,
            ),
            secondary_window=RateLimitWindow(
                used_percent=29,
                resets_at="2026-04-10T00:00:00Z",
                window_duration_mins=10080,
            ),
            credits_has_credits=None,
            credits_unlimited=None,
            credits_balance=None,
            observed_at=stale_observed_at,
        )
    )
    state.save(AppState(active_alias="beta", updated_at="2026-04-06T00:00:00Z"))

    entries, active_alias = manager.list_aliases()

    assert entries == [
        AliasListEntry(
            alias="beta",
            plan_type="plus",
            five_hour_left_percent=88,
            weekly_left_percent=12,
        )
    ]
    assert active_alias == "beta"
    assert probe_calls == ["beta"]


def test_list_aliases_hides_stale_usage_when_refresh_fails(tmp_path):
    stale_observed_at = make_observed_at(minutes_ago=30)

    def alias_metadata_probe(_alias: str) -> AliasTelemetryObservation | None:
        raise RuntimeError("rpc unavailable")

    manager, _paths, accounts, state, store, _guard = make_manager(
        tmp_path,
        alias_metadata_probe=alias_metadata_probe,
    )
    accounts.write_snapshot_from_bytes("beta", b"{}")
    store.reconcile_aliases(["beta"])
    store.record_alias_observation(
        alias="beta",
        account_email="beta@example.com",
        account_plan_type="plus",
        account_fingerprint="fp-beta",
        observed_at=stale_observed_at,
    )
    store.upsert_rate_limit(
        RateLimitSnapshot(
            alias="beta",
            limit_id="codex",
            limit_name="codex",
            observed_via=UsageSource.RPC,
            plan_type="plus",
            primary_window=RateLimitWindow(
                used_percent=58,
                resets_at="2026-04-06T05:00:00Z",
                window_duration_mins=300,
            ),
            secondary_window=RateLimitWindow(
                used_percent=29,
                resets_at="2026-04-10T00:00:00Z",
                window_duration_mins=10080,
            ),
            credits_has_credits=None,
            credits_unlimited=None,
            credits_balance=None,
            observed_at=stale_observed_at,
        )
    )
    state.save(AppState(active_alias="beta", updated_at="2026-04-06T00:00:00Z"))

    entries, active_alias = manager.list_aliases()

    assert entries == [
        AliasListEntry(
            alias="beta",
            plan_type="plus",
            five_hour_left_percent=None,
            weekly_left_percent=None,
        )
    ]
    assert active_alias == "beta"


def test_list_aliases_refreshes_stale_inactive_usage_without_mutation_guard(tmp_path):
    probe_calls: list[str] = []
    stale_observed_at = make_observed_at(minutes_ago=30)
    fresh_observed_at = make_observed_at(minutes_ago=1)

    def alias_metadata_probe(alias: str) -> AliasTelemetryObservation | None:
        probe_calls.append(alias)
        return AliasTelemetryObservation(
            account_email="backup@example.com",
            account_plan_type="pro",
            account_fingerprint="fp-backup",
            observed_at=fresh_observed_at,
            rate_limits=(
                RateLimitSnapshot(
                    alias=alias,
                    limit_id="codex",
                    limit_name="codex",
                    observed_via=UsageSource.RPC,
                    plan_type="pro",
                    primary_window=RateLimitWindow(
                        used_percent=12,
                        resets_at="2026-04-06T05:00:00Z",
                        window_duration_mins=300,
                    ),
                    secondary_window=RateLimitWindow(
                        used_percent=88,
                        resets_at="2026-04-10T00:00:00Z",
                        window_duration_mins=10080,
                    ),
                    credits_has_credits=None,
                    credits_unlimited=None,
                    credits_balance=None,
                    observed_at=fresh_observed_at,
                ),
            ),
        )

    manager, _paths, accounts, state, store, _guard = make_manager(
        tmp_path,
        alias_metadata_probe=alias_metadata_probe,
    )

    def failing_guard() -> None:
        raise RuntimeError("codex is running")

    manager._ensure_safe_to_mutate = failing_guard
    accounts.write_snapshot_from_bytes("backup", b"{}")
    store.reconcile_aliases(["backup"])
    store.record_alias_observation(
        alias="backup",
        account_email="backup@example.com",
        account_plan_type="pro",
        account_fingerprint="fp-backup",
        observed_at=stale_observed_at,
    )
    store.upsert_rate_limit(
        RateLimitSnapshot(
            alias="backup",
            limit_id="codex",
            limit_name="codex",
            observed_via=UsageSource.RPC,
            plan_type="pro",
            primary_window=RateLimitWindow(
                used_percent=58,
                resets_at="2026-04-06T05:00:00Z",
                window_duration_mins=300,
            ),
            secondary_window=RateLimitWindow(
                used_percent=29,
                resets_at="2026-04-10T00:00:00Z",
                window_duration_mins=10080,
            ),
            credits_has_credits=None,
            credits_unlimited=None,
            credits_balance=None,
            observed_at=stale_observed_at,
        )
    )
    state.save(AppState(active_alias=None, updated_at="2026-04-05T00:00:00Z"))

    entries, active_alias = manager.list_aliases()

    assert entries == [
        AliasListEntry(
            alias="backup",
            plan_type="pro",
            five_hour_left_percent=88,
            weekly_left_percent=12,
        )
    ]
    assert active_alias is None
    assert probe_calls == ["backup"]


def test_list_aliases_includes_cached_remaining_usage(tmp_path):
    observed_at = make_observed_at(minutes_ago=1)
    manager, _paths, accounts, state, store, _guard = make_manager(tmp_path)
    accounts.write_snapshot_from_bytes("beta", b"{}")
    store.reconcile_aliases(["beta"])
    store.record_alias_observation(
        alias="beta",
        account_email="beta@example.com",
        account_plan_type="plus",
        account_fingerprint="fp-beta",
        observed_at=observed_at,
    )
    store.upsert_rate_limit(
        RateLimitSnapshot(
            alias="beta",
            limit_id="codex",
            limit_name="codex",
            observed_via=UsageSource.RPC,
            plan_type="plus",
            primary_window=RateLimitWindow(
                used_percent=58,
                resets_at="2026-04-06T05:00:00Z",
                window_duration_mins=300,
            ),
            secondary_window=RateLimitWindow(
                used_percent=29,
                resets_at="2026-04-10T00:00:00Z",
                window_duration_mins=10080,
            ),
            credits_has_credits=None,
            credits_unlimited=None,
            credits_balance=None,
            observed_at=observed_at,
        )
    )
    state.save(AppState(active_alias="beta", updated_at="2026-04-06T00:00:00Z"))

    entries, active_alias = manager.list_aliases()

    assert entries == [
        AliasListEntry(
            alias="beta",
            plan_type="plus",
            five_hour_left_percent=42,
            weekly_left_percent=71,
        )
    ]
    assert active_alias == "beta"


def test_list_aliases_prefers_newest_rate_limit_snapshot_over_older_codex_snapshot(tmp_path):
    older_observed_at = make_observed_at(minutes_ago=2)
    newer_observed_at = make_observed_at(minutes_ago=1)
    manager, _paths, accounts, state, store, _guard = make_manager(tmp_path)
    accounts.write_snapshot_from_bytes("beta", b"{}")
    store.reconcile_aliases(["beta"])
    store.record_alias_observation(
        alias="beta",
        account_email="beta@example.com",
        account_plan_type="plus",
        account_fingerprint="fp-beta",
        observed_at=newer_observed_at,
    )
    store.upsert_rate_limit(
        RateLimitSnapshot(
            alias="beta",
            limit_id="codex",
            limit_name="codex",
            observed_via=UsageSource.RPC,
            plan_type="plus",
            primary_window=RateLimitWindow(
                used_percent=58,
                resets_at="2026-04-06T05:00:00Z",
                window_duration_mins=300,
            ),
            secondary_window=RateLimitWindow(
                used_percent=29,
                resets_at="2026-04-10T00:00:00Z",
                window_duration_mins=10080,
            ),
            credits_has_credits=None,
            credits_unlimited=None,
            credits_balance=None,
            observed_at=older_observed_at,
        )
    )
    store.upsert_rate_limit(
        RateLimitSnapshot(
            alias="beta",
            limit_id="aaa-other",
            limit_name="other",
            observed_via=UsageSource.RPC,
            plan_type="plus",
            primary_window=RateLimitWindow(
                used_percent=11,
                resets_at="2026-04-06T05:00:00Z",
                window_duration_mins=300,
            ),
            secondary_window=RateLimitWindow(
                used_percent=80,
                resets_at="2026-04-10T00:00:00Z",
                window_duration_mins=10080,
            ),
            credits_has_credits=None,
            credits_unlimited=None,
            credits_balance=None,
            observed_at=newer_observed_at,
        )
    )
    state.save(AppState(active_alias="beta", updated_at="2026-04-06T00:00:00Z"))

    entries, active_alias = manager.list_aliases()

    assert entries == [
        AliasListEntry(
            alias="beta",
            plan_type="plus",
            five_hour_left_percent=89,
            weekly_left_percent=20,
        )
    ]
    assert active_alias == "beta"


def test_list_aliases_prefers_codex_snapshot_when_same_timestamp_snapshots_exist(tmp_path):
    observed_at = make_observed_at(minutes_ago=1)
    manager, _paths, accounts, state, store, _guard = make_manager(tmp_path)
    accounts.write_snapshot_from_bytes("beta", b"{}")
    store.reconcile_aliases(["beta"])
    store.record_alias_observation(
        alias="beta",
        account_email="beta@example.com",
        account_plan_type="plus",
        account_fingerprint="fp-beta",
        observed_at=observed_at,
    )
    store.upsert_rate_limit(
        RateLimitSnapshot(
            alias="beta",
            limit_id="aaa-other",
            limit_name="other",
            observed_via=UsageSource.RPC,
            plan_type="plus",
            primary_window=RateLimitWindow(
                used_percent=10,
                resets_at="2026-04-06T05:00:00Z",
                window_duration_mins=300,
            ),
            secondary_window=RateLimitWindow(
                used_percent=20,
                resets_at="2026-04-10T00:00:00Z",
                window_duration_mins=10080,
            ),
            credits_has_credits=None,
            credits_unlimited=None,
            credits_balance=None,
            observed_at=observed_at,
        )
    )
    store.upsert_rate_limit(
        RateLimitSnapshot(
            alias="beta",
            limit_id="codex",
            limit_name="codex",
            observed_via=UsageSource.RPC,
            plan_type="plus",
            primary_window=RateLimitWindow(
                used_percent=58,
                resets_at="2026-04-06T05:00:00Z",
                window_duration_mins=300,
            ),
            secondary_window=RateLimitWindow(
                used_percent=29,
                resets_at="2026-04-10T00:00:00Z",
                window_duration_mins=10080,
            ),
            credits_has_credits=None,
            credits_unlimited=None,
            credits_balance=None,
            observed_at=observed_at,
        )
    )
    state.save(AppState(active_alias="beta", updated_at="2026-04-06T00:00:00Z"))

    entries, active_alias = manager.list_aliases()

    assert entries == [
        AliasListEntry(
            alias="beta",
            plan_type="plus",
            five_hour_left_percent=42,
            weekly_left_percent=71,
        )
    ]
    assert active_alias == "beta"


def test_list_aliases_uses_cached_rate_limit_plan_type_when_alias_metadata_is_missing(tmp_path):
    observed_at = make_observed_at(minutes_ago=1)
    manager, _paths, accounts, state, store, _guard = make_manager(tmp_path)
    accounts.write_snapshot_from_bytes("beta", b"{}")
    store.upsert_rate_limit(
        RateLimitSnapshot(
            alias="beta",
            limit_id="codex",
            limit_name="codex",
            observed_via=UsageSource.PTY,
            plan_type="pro",
            primary_window=RateLimitWindow(
                used_percent=34,
                resets_at="2026-04-06T05:00:00Z",
                window_duration_mins=300,
            ),
            secondary_window=RateLimitWindow(
                used_percent=67,
                resets_at="2026-04-10T00:00:00Z",
                window_duration_mins=10080,
            ),
            credits_has_credits=None,
            credits_unlimited=None,
            credits_balance=None,
            observed_at=observed_at,
        )
    )
    state.save(AppState(active_alias="beta", updated_at="2026-04-06T00:00:00Z"))

    assert store.list_aliases() == []

    entries, active_alias = manager.list_aliases()

    assert entries == [
        AliasListEntry(
            alias="beta",
            plan_type="pro",
            five_hour_left_percent=66,
            weekly_left_percent=33,
        )
    ]
    assert active_alias == "beta"


def test_list_aliases_refreshes_missing_usage_and_persists_rate_limits(tmp_path):
    observed_at = make_observed_at(minutes_ago=1)

    def alias_metadata_probe(alias: str) -> AliasTelemetryObservation | None:
        return AliasTelemetryObservation(
            account_email=f"{alias}@example.com",
            account_plan_type="plus",
            account_fingerprint=f"fp-{alias}",
            observed_at=observed_at,
            rate_limits=(
                RateLimitSnapshot(
                    alias=alias,
                    limit_id="codex",
                    limit_name="codex",
                    observed_via=UsageSource.RPC,
                    plan_type="plus",
                    primary_window=RateLimitWindow(
                        used_percent=58,
                        resets_at="2026-04-06T05:00:00Z",
                        window_duration_mins=300,
                    ),
                    secondary_window=RateLimitWindow(
                        used_percent=29,
                        resets_at="2026-04-10T00:00:00Z",
                        window_duration_mins=10080,
                    ),
                    credits_has_credits=None,
                    credits_unlimited=None,
                    credits_balance=None,
                    observed_at=observed_at,
                ),
            ),
        )

    manager, _paths, accounts, state, store, _guard = make_manager(
        tmp_path,
        alias_metadata_probe=alias_metadata_probe,
    )
    accounts.write_snapshot_from_bytes("beta", b"{}")
    store.reconcile_aliases(["beta"])
    state.save(AppState(active_alias="beta", updated_at="2026-04-06T00:00:00Z"))

    entries, _active_alias = manager.list_aliases()

    assert entries == [
        AliasListEntry(
            alias="beta",
            plan_type="plus",
            five_hour_left_percent=42,
            weekly_left_percent=71,
        )
    ]
    latest = store.latest_rate_limit_for_alias("beta")
    assert latest is not None
    assert latest.primary_used_percent == 58
    assert latest.secondary_used_percent == 29


def test_list_aliases_refreshes_usage_without_plan_type_and_persists_rate_limits(tmp_path):
    observed_at = make_observed_at(minutes_ago=1)

    def alias_metadata_probe(alias: str) -> AliasTelemetryObservation | None:
        return AliasTelemetryObservation(
            account_email=None,
            account_plan_type=None,
            account_fingerprint=None,
            observed_at=observed_at,
            rate_limits=(
                RateLimitSnapshot(
                    alias=alias,
                    limit_id="codex",
                    limit_name="codex",
                    observed_via=UsageSource.PTY,
                    plan_type=None,
                    primary_window=RateLimitWindow(
                        used_percent=34,
                        resets_at="2026-04-06T05:00:00Z",
                        window_duration_mins=300,
                    ),
                    secondary_window=RateLimitWindow(
                        used_percent=67,
                        resets_at="2026-04-10T00:00:00Z",
                        window_duration_mins=10080,
                    ),
                    credits_has_credits=None,
                    credits_unlimited=None,
                    credits_balance=None,
                    observed_at=observed_at,
                ),
            ),
        )

    manager, _paths, accounts, state, store, _guard = make_manager(
        tmp_path,
        alias_metadata_probe=alias_metadata_probe,
    )
    accounts.write_snapshot_from_bytes("beta", b"{}")
    store.reconcile_aliases(["beta"])
    state.save(AppState(active_alias="beta", updated_at="2026-04-06T00:00:00Z"))

    entries, active_alias = manager.list_aliases()

    assert entries == [
        AliasListEntry(
            alias="beta",
            plan_type=None,
            five_hour_left_percent=66,
            weekly_left_percent=33,
        )
    ]
    assert active_alias == "beta"
    latest = store.latest_rate_limit_for_alias("beta")
    assert latest is not None
    assert latest.observed_via == UsageSource.PTY
    assert latest.primary_used_percent == 34
    assert latest.secondary_used_percent == 67


def test_list_aliases_refreshes_missing_usage_for_inactive_alias_and_restores_auth(tmp_path):
    observed_at = make_observed_at(minutes_ago=1)

    def alias_metadata_probe(alias: str) -> AliasTelemetryObservation | None:
        if alias != "backup":
            return None
        return AliasTelemetryObservation(
            account_email="backup@example.com",
            account_plan_type="pro",
            account_fingerprint="fp-backup",
            observed_at=observed_at,
            rate_limits=(
                RateLimitSnapshot(
                    alias="backup",
                    limit_id="codex",
                    limit_name="codex",
                    observed_via=UsageSource.RPC,
                    plan_type="pro",
                    primary_window=RateLimitWindow(
                        used_percent=12,
                        resets_at="2026-04-06T05:00:00Z",
                        window_duration_mins=300,
                    ),
                    secondary_window=RateLimitWindow(
                        used_percent=88,
                        resets_at="2026-04-10T00:00:00Z",
                        window_duration_mins=10080,
                    ),
                    credits_has_credits=None,
                    credits_unlimited=None,
                    credits_balance=None,
                    observed_at=observed_at,
                ),
            ),
        )

    manager, paths, accounts, state, store, guard = make_manager(
        tmp_path,
        alias_metadata_probe=alias_metadata_probe,
    )
    accounts.write_snapshot_from_bytes("work", b'{"token":"snapshot-work"}')
    accounts.write_snapshot_from_bytes("backup", b'{"token":"snapshot-backup"}')
    store.reconcile_aliases(["backup", "work"])
    state.save(AppState(active_alias="work", updated_at="2026-04-06T00:00:00Z"))
    paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.live_auth_file.write_bytes(b'{"token":"live-work"}')

    entries, active_alias = manager.list_aliases()

    assert entries == [
        AliasListEntry(
            alias="backup",
            plan_type="pro",
            five_hour_left_percent=88,
            weekly_left_percent=12,
        ),
        AliasListEntry(
            alias="work",
            plan_type=None,
            five_hour_left_percent=None,
            weekly_left_percent=None,
        ),
    ]
    assert active_alias == "work"
    assert guard.calls == 0
    assert paths.live_auth_file.read_bytes() == b'{"token":"live-work"}'


def test_list_aliases_inactive_refresh_no_longer_mutates_live_auth(tmp_path, monkeypatch):
    manager, _paths, accounts, state, store, _guard = make_manager(
        tmp_path,
        alias_metadata_probe=lambda _alias: None,
    )
    accounts.write_snapshot_from_bytes("backup", b"{}")
    store.reconcile_aliases(["backup"])
    state.save(AppState(active_alias=None, updated_at="2026-04-13T09:00:00Z"))

    backup_calls = 0

    def fail_backup():
        nonlocal backup_calls
        backup_calls += 1
        raise AssertionError("list refresh should not back up live auth")

    monkeypatch.setattr(manager, "_backup_live_auth", fail_backup)

    entries, active_alias = manager.list_aliases()

    assert entries == [
        AliasListEntry(
            alias="backup",
            plan_type=None,
            five_hour_left_percent=None,
            weekly_left_percent=None,
        )
    ]
    assert active_alias is None
    assert backup_calls == 0
