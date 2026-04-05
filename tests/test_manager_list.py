from __future__ import annotations

import pytest

from codex_switch.accounts import AccountStore
from codex_switch.automation_db import AutomationStore
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
        AliasListEntry(alias="backup", plan_type=None),
        AliasListEntry(alias="beta", plan_type="plus"),
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
        AliasListEntry(alias="backup", plan_type=None),
        AliasListEntry(alias="beta", plan_type=None),
    ]
    assert active_alias == "beta"
    assert not store._db_file.exists()


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
        AliasListEntry(alias="backup", plan_type=None),
        AliasListEntry(alias="beta", plan_type=None),
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

    assert entries == [AliasListEntry(alias="beta", plan_type="plus")]
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
        AliasListEntry(alias="backup", plan_type="pro"),
        AliasListEntry(alias="work", plan_type=None),
    ]
    assert active_alias == "work"
    assert probe_calls == ["backup", "work"]
    assert guard.calls == 1
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


def test_list_aliases_raises_when_inactive_probe_cleanup_fails(tmp_path, monkeypatch):
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

    monkeypatch.setattr(
        manager,
        "_restore_previous_live_auth",
        lambda _previous_state, _backup_path, _clear_unmanaged_live_auth, prefer_live_backup=False: (
            (_ for _ in ()).throw(RuntimeError("restore failed"))
        ),
    )

    with pytest.raises(RuntimeError, match="restore failed"):
        manager.list_aliases()


def test_list_aliases_skips_inactive_refresh_when_mutation_is_unsafe(tmp_path):
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

    assert entries == [AliasListEntry(alias="backup", plan_type=None)]
    assert active_alias is None
    assert probe_calls == []


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

    assert entries == [AliasListEntry(alias="backup", plan_type=None)]
    assert active_alias == "backup"
