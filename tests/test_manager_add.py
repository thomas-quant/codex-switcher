from __future__ import annotations

from pathlib import Path

import pytest

from codex_switch.accounts import AccountStore
from codex_switch.errors import AliasAlreadyExistsError, CodexProcessRunningError, LoginCaptureError
from codex_switch.manager import CodexSwitchManager
from codex_switch.models import AppState, LoginMode
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
    def login_runner(login_mode: LoginMode, *, env: dict[str, str] | None = None) -> None:
        assert login_mode == LoginMode.BROWSER
        assert env is None
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
    manager, paths, accounts, state, guard = make_manager(
        tmp_path,
        lambda _login_mode, *, env=None: None,
    )
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

    def login_runner(login_mode: LoginMode, *, env: dict[str, str] | None = None) -> None:
        nonlocal login_calls
        assert login_mode == LoginMode.BROWSER
        assert env is None
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
    def login_runner(login_mode: LoginMode, *, env: dict[str, str] | None = None) -> None:
        assert login_mode == LoginMode.BROWSER
        assert env is None
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
    manager, paths, accounts, state, guard = make_manager(
        tmp_path,
        lambda _login_mode, *, env=None: None,
    )
    state.save(AppState(active_alias=None, updated_at="2026-03-31T12:00:00Z"))
    paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.live_auth_file.write_bytes(b'{"token":"live-before-login"}')

    def fail_restore(_previous_state, _backup_path, _clear_unmanaged_live_auth):
        raise RuntimeError("restore failed")

    monkeypatch.setattr(manager, "_restore_previous_live_auth", fail_restore)

    with pytest.raises(
        LoginCaptureError,
        match=r"codex login did not leave ~/.codex/auth.json behind",
    ) as exc_info:
        manager.add("personal")

    assert guard.calls == 1
    assert exc_info.value.__notes__ == ["Cleanup failed: restore failed"]


def test_add_preserves_live_auth_when_backup_creation_fails(tmp_path, monkeypatch):
    manager, paths, accounts, state, guard = make_manager(
        tmp_path,
        lambda _login_mode, *, env=None: None,
    )
    state.save(AppState(active_alias=None, updated_at="2026-03-31T12:00:00Z"))
    paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.live_auth_file.write_bytes(b'{"token":"live-before-backup"}')

    def fail_backup():
        raise OSError("backup failed")

    monkeypatch.setattr(manager, "_backup_live_auth", fail_backup)

    with pytest.raises(OSError, match="backup failed"):
        manager.add("personal")

    assert guard.calls == 1
    assert paths.live_auth_file.read_bytes() == b'{"token":"live-before-backup"}'
    assert state.load() == AppState(active_alias=None, updated_at="2026-03-31T12:00:00Z")
    assert not accounts.exists("personal")
    assert sorted(path.name for path in paths.live_auth_file.parent.iterdir()) == ["auth.json"]


def test_add_rolls_back_captured_alias_when_cleanup_fails(tmp_path, monkeypatch):
    def login_runner(login_mode: LoginMode, *, env: dict[str, str] | None = None) -> None:
        assert login_mode == LoginMode.BROWSER
        assert env is None
        paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
        paths.live_auth_file.write_bytes(b'{"token":"new-login"}')

    manager, paths, accounts, state, guard = make_manager(tmp_path, login_runner)
    state.save(AppState(active_alias=None, updated_at="2026-03-31T12:00:00Z"))
    paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.live_auth_file.write_bytes(b'{"token":"live-before-login"}')

    def fail_restore(_previous_state, _backup_path, _clear_unmanaged_live_auth):
        raise RuntimeError("restore failed")

    monkeypatch.setattr(manager, "_restore_previous_live_auth", fail_restore)

    with pytest.raises(RuntimeError, match="restore failed"):
        manager.add("personal")

    assert guard.calls == 1
    assert not accounts.exists("personal")


def test_add_rolls_back_alias_when_snapshot_write_raises_after_writing(tmp_path, monkeypatch):
    def login_runner(login_mode: LoginMode, *, env: dict[str, str] | None = None) -> None:
        assert login_mode == LoginMode.BROWSER
        assert env is None
        paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
        paths.live_auth_file.write_bytes(b'{"token":"new-login"}')

    manager, paths, accounts, state, guard = make_manager(tmp_path, login_runner)
    state.save(AppState(active_alias=None, updated_at="2026-03-31T12:00:00Z"))
    paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.live_auth_file.write_bytes(b'{"token":"live-before-login"}')

    original_write = accounts.write_snapshot_from_file

    def write_then_fail(alias, source):
        original_write(alias, source)
        raise OSError("fsync failed")

    monkeypatch.setattr(accounts, "write_snapshot_from_file", write_then_fail)

    with pytest.raises(OSError, match="fsync failed"):
        manager.add("personal")

    assert guard.calls == 1
    assert not accounts.exists("personal")


def test_add_isolated_captures_new_alias_without_touching_live_auth_or_state(tmp_path):
    isolated_envs: list[dict[str, str]] = []

    def login_runner(login_mode: LoginMode, *, env: dict[str, str] | None = None) -> None:
        assert login_mode == LoginMode.BROWSER
        assert env is not None
        isolated_envs.append(env)
        auth_file = Path(env["CODEX_HOME"]) / "auth.json"
        auth_file.parent.mkdir(parents=True, exist_ok=True)
        auth_file.write_bytes(b'{"token":"isolated-login"}')

    manager, paths, accounts, state, guard = make_manager(tmp_path, login_runner)
    accounts.write_snapshot_from_bytes("work", b'{"token":"snapshot-work"}')
    state.save(AppState(active_alias="work", updated_at="2026-03-31T12:00:00Z"))
    paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.live_auth_file.write_bytes(b'{"token":"live-work"}')

    manager.add("personal", isolated=True)

    assert guard.calls == 0
    assert len(isolated_envs) == 1
    assert accounts.read_snapshot("personal") == b'{"token":"isolated-login"}'
    assert accounts.read_snapshot("work") == b'{"token":"snapshot-work"}'
    assert paths.live_auth_file.read_bytes() == b'{"token":"live-work"}'
    assert state.load() == AppState(active_alias="work", updated_at="2026-03-31T12:00:00Z")


def test_add_passes_selected_login_mode_to_runner(tmp_path):
    login_modes: list[tuple[LoginMode, bool]] = []

    def login_runner(login_mode: LoginMode, *, env: dict[str, str] | None = None) -> None:
        login_modes.append((login_mode, env is not None))
        paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
        target = Path(paths.live_auth_file if env is None else Path(env["CODEX_HOME"]) / "auth.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'{"token":"device-auth"}')

    manager, paths, accounts, state, guard = make_manager(tmp_path, login_runner)
    state.save(AppState(active_alias=None, updated_at="2026-03-31T12:00:00Z"))

    manager.add("personal", login_mode=LoginMode.DEVICE_AUTH)

    assert guard.calls == 1
    assert login_modes == [(LoginMode.DEVICE_AUTH, False)]
    assert accounts.read_snapshot("personal") == b'{"token":"device-auth"}'


def test_add_isolated_passes_device_auth_mode(tmp_path):
    login_modes: list[tuple[LoginMode, bool]] = []

    def login_runner(login_mode: LoginMode, *, env: dict[str, str] | None = None) -> None:
        login_modes.append((login_mode, env is not None))
        assert env is not None
        auth_file = Path(env["CODEX_HOME"]) / "auth.json"
        auth_file.parent.mkdir(parents=True, exist_ok=True)
        auth_file.write_bytes(b'{"token":"device-auth"}')

    manager, _paths, accounts, state, guard = make_manager(tmp_path, login_runner)
    state.save(AppState(active_alias=None, updated_at="2026-03-31T12:00:00Z"))

    manager.add("personal", login_mode=LoginMode.DEVICE_AUTH, isolated=True)

    assert guard.calls == 0
    assert login_modes == [(LoginMode.DEVICE_AUTH, True)]
    assert accounts.read_snapshot("personal") == b'{"token":"device-auth"}'


def test_add_process_running_error_suggests_isolated(tmp_path):
    manager, _paths, _accounts, state, _guard = make_manager(
        tmp_path,
        lambda _login_mode, *, env=None: None,
    )
    state.save(AppState(active_alias=None, updated_at="2026-03-31T12:00:00Z"))

    def running_guard() -> None:
        raise CodexProcessRunningError(
            "A codex process is running. Exit Codex before mutating account state."
        )

    manager._ensure_safe_to_mutate = running_guard

    with pytest.raises(
        CodexProcessRunningError,
        match=r"codex-switch add personal --isolated",
    ):
        manager.add("personal")
