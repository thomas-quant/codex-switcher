from __future__ import annotations

import pytest

from codex_switch.accounts import AccountStore
from codex_switch.errors import AliasAlreadyExistsError, LoginCaptureError
from codex_switch.manager import CodexSwitchManager
from codex_switch.models import AppState
from codex_switch.paths import resolve_paths
from codex_switch.state import StateStore


class MutationGuardSpy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def make_manager(tmp_path, login_runner):
    paths = resolve_paths(tmp_path)
    accounts = AccountStore(paths.accounts_dir)
    state = StateStore(paths.state_file)
    guard = MutationGuardSpy()
    manager = CodexSwitchManager(
        paths=paths,
        accounts=accounts,
        state=state,
        ensure_safe_to_mutate=guard,
        login_runner=login_runner,
    )
    return manager, paths, accounts, state, guard


def test_add_captures_new_alias_and_restores_previous_active_auth(tmp_path):
    def login_runner() -> None:
        paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
        paths.live_auth_file.write_bytes(b'{"token":"new-login"}')

    manager, paths, accounts, state, guard = make_manager(tmp_path, login_runner)
    accounts.write_snapshot_from_bytes("work", b'{"token":"snapshot-work"}')
    state.save(AppState(active_alias="work", updated_at="2026-03-31T12:00:00Z"))
    paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.live_auth_file.write_bytes(b'{"token":"live-work"}')

    manager.add("personal")

    assert guard.calls == 1
    assert accounts.read_snapshot("work") == b'{"token":"live-work"}'
    assert accounts.read_snapshot("personal") == b'{"token":"new-login"}'
    assert paths.live_auth_file.read_bytes() == b'{"token":"live-work"}'
    assert state.load() == AppState(active_alias="work", updated_at="2026-03-31T12:00:00Z")


def test_add_rolls_back_when_login_does_not_produce_auth(tmp_path):
    manager, paths, accounts, state, guard = make_manager(tmp_path, lambda: None)
    accounts.write_snapshot_from_bytes("work", b'{"token":"snapshot-work"}')
    state.save(AppState(active_alias="work", updated_at="2026-03-31T12:00:00Z"))
    paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.live_auth_file.write_bytes(b'{"token":"live-work"}')

    with pytest.raises(
        LoginCaptureError,
        match=r"codex login did not leave ~/.codex/auth.json behind",
    ):
        manager.add("personal")

    assert guard.calls == 1
    assert not accounts.exists("personal")
    assert accounts.read_snapshot("work") == b'{"token":"live-work"}'
    assert paths.live_auth_file.read_bytes() == b'{"token":"live-work"}'
    assert state.load() == AppState(active_alias="work", updated_at="2026-03-31T12:00:00Z")
    assert sorted(path.name for path in paths.live_auth_file.parent.iterdir()) == ["auth.json"]


def test_add_rejects_existing_alias(tmp_path):
    login_calls = 0

    def login_runner() -> None:
        nonlocal login_calls
        login_calls += 1

    manager, paths, accounts, state, guard = make_manager(tmp_path, login_runner)
    accounts.write_snapshot_from_bytes("personal", b'{"token":"existing"}')
    state.save(AppState(active_alias=None, updated_at="2026-03-31T12:00:00Z"))
    paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.live_auth_file.write_bytes(b'{"token":"live-work"}')

    with pytest.raises(AliasAlreadyExistsError, match="Alias 'personal' already exists"):
        manager.add("personal")

    assert guard.calls == 1
    assert login_calls == 0
    assert accounts.read_snapshot("personal") == b'{"token":"existing"}'
    assert paths.live_auth_file.read_bytes() == b'{"token":"live-work"}'
    assert state.load() == AppState(active_alias=None, updated_at="2026-03-31T12:00:00Z")


def test_add_restores_backup_when_no_active_alias(tmp_path):
    def login_runner() -> None:
        paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
        paths.live_auth_file.write_bytes(b'{"token":"new-login"}')

    manager, paths, accounts, state, guard = make_manager(tmp_path, login_runner)
    state.save(AppState(active_alias=None, updated_at="2026-03-31T12:00:00Z"))
    paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.live_auth_file.write_bytes(b'{"token":"live-before-login"}')

    manager.add("personal")

    assert guard.calls == 1
    assert accounts.read_snapshot("personal") == b'{"token":"new-login"}'
    assert paths.live_auth_file.read_bytes() == b'{"token":"live-before-login"}'
    assert state.load() == AppState(active_alias=None, updated_at="2026-03-31T12:00:00Z")
    assert sorted(path.name for path in paths.live_auth_file.parent.iterdir()) == ["auth.json"]


def test_add_preserves_login_failure_when_restore_also_fails(tmp_path, monkeypatch):
    manager, paths, accounts, state, guard = make_manager(tmp_path, lambda: None)
    state.save(AppState(active_alias=None, updated_at="2026-03-31T12:00:00Z"))
    paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.live_auth_file.write_bytes(b'{"token":"live-before-login"}')

    def fail_restore(_previous_state, _backup_path):
        raise RuntimeError("restore failed")

    monkeypatch.setattr(manager, "_restore_previous_live_auth", fail_restore)

    with pytest.raises(
        LoginCaptureError,
        match=r"codex login did not leave ~/.codex/auth.json behind",
    ) as exc_info:
        manager.add("personal")

    assert guard.calls == 1
    assert exc_info.value.__notes__ == ["Cleanup failed: restore failed"]
