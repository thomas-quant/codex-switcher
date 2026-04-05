from __future__ import annotations

import pytest

from codex_switch.accounts import AccountStore
from codex_switch.errors import ActiveAliasRemovalError, SnapshotNotFoundError
from codex_switch.manager import CodexSwitchManager
from codex_switch.models import AliasListEntry, AppState, StatusResult
from codex_switch.paths import resolve_paths
from codex_switch.state import StateStore


class MutationGuardSpy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def make_manager(tmp_path):
    paths = resolve_paths(tmp_path)
    accounts = AccountStore(paths.accounts_dir)
    state = StateStore(paths.state_file)
    guard = MutationGuardSpy()
    manager = CodexSwitchManager(
        paths=paths,
        accounts=accounts,
        state=state,
        ensure_safe_to_mutate=guard,
        login_runner=lambda _mode: None,
    )
    return manager, paths, accounts, state, guard


def test_use_syncs_current_alias_before_switching(tmp_path):
    manager, paths, accounts, state, guard = make_manager(tmp_path)
    accounts.write_snapshot_from_bytes("work", b'{"token":"snapshot-work"}')
    accounts.write_snapshot_from_bytes("personal", b'{"token":"personal"}')
    state.save(AppState(active_alias="work", updated_at="2026-03-31T12:00:00Z"))
    paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.live_auth_file.write_bytes(b'{"token":"live-work"}')

    manager.use("personal")

    assert guard.calls == 1
    assert accounts.read_snapshot("work") == b'{"token":"live-work"}'
    assert paths.live_auth_file.read_bytes() == b'{"token":"personal"}'
    assert state.load().active_alias == "personal"
    assert state.load().updated_at is not None


def test_use_missing_alias_does_not_mutate_current_snapshot(tmp_path):
    manager, paths, accounts, state, guard = make_manager(tmp_path)
    accounts.write_snapshot_from_bytes("work", b'{"token":"snapshot-work"}')
    state.save(AppState(active_alias="work", updated_at="2026-03-31T12:00:00Z"))
    paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.live_auth_file.write_bytes(b'{"token":"live-work"}')

    with pytest.raises(SnapshotNotFoundError, match="Alias 'missing' does not exist"):
        manager.use("missing")

    assert guard.calls == 1
    assert accounts.read_snapshot("work") == b'{"token":"snapshot-work"}'
    assert paths.live_auth_file.read_bytes() == b'{"token":"live-work"}'
    assert state.load() == AppState(active_alias="work", updated_at="2026-03-31T12:00:00Z")


def test_status_reports_dirty_live_auth(tmp_path):
    manager, paths, accounts, state, _guard = make_manager(tmp_path)
    accounts.write_snapshot_from_bytes("work", b'{"token":"snapshot-work"}')
    state.save(AppState(active_alias="work", updated_at="2026-03-31T12:00:00Z"))
    paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.live_auth_file.write_bytes(b'{"token":"live-work"}')

    assert manager.status() == StatusResult(
        active_alias="work",
        snapshot_exists=True,
        live_auth_exists=True,
        in_sync=False,
    )


def test_remove_rejects_active_alias(tmp_path):
    manager, _paths, accounts, state, guard = make_manager(tmp_path)
    accounts.write_snapshot_from_bytes("work", b'{"token":"work"}')
    state.save(AppState(active_alias="work", updated_at="2026-03-31T12:00:00Z"))

    with pytest.raises(ActiveAliasRemovalError, match="Cannot remove active alias 'work'"):
        manager.remove("work")

    assert guard.calls == 1
    assert accounts.exists("work")


def test_list_aliases_returns_sorted_names_and_active_alias(tmp_path):
    manager, _paths, accounts, state, _guard = make_manager(tmp_path)
    accounts.write_snapshot_from_bytes("zeta", b"{}")
    accounts.write_snapshot_from_bytes("alpha", b"{}")
    state.save(AppState(active_alias="zeta", updated_at="2026-03-31T12:00:00Z"))

    aliases, active_alias = manager.list_aliases()

    assert aliases == [
        AliasListEntry(alias="alpha", plan_type=None),
        AliasListEntry(alias="zeta", plan_type=None),
    ]
    assert active_alias == "zeta"
