from __future__ import annotations

from codex_switch.accounts import AccountStore
from codex_switch.automation_db import AutomationStore
from codex_switch.manager import CodexSwitchManager
from codex_switch.models import AliasListEntry, AppState
from codex_switch.paths import resolve_paths
from codex_switch.state import StateStore


class MutationGuardSpy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def make_manager(tmp_path, *, initialize_automation: bool = True):
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
    )
    return manager, accounts, state, store


def test_list_aliases_uses_cached_plan_types(tmp_path):
    manager, accounts, state, store = make_manager(tmp_path)
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
    manager, accounts, state, store = make_manager(tmp_path, initialize_automation=False)
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
    manager, accounts, state, store = make_manager(tmp_path)
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
