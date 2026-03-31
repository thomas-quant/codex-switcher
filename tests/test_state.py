import errno
import json
import os
import stat
from pathlib import Path

import pytest

from codex_switch.errors import StateFileError
import codex_switch.fs as fs
from codex_switch.models import AppState
from codex_switch.paths import resolve_paths
from codex_switch.state import StateStore


def test_state_store_returns_default_when_file_is_missing(tmp_path):
    paths = resolve_paths(tmp_path)
    store = StateStore(paths.state_file)

    assert store.load() == AppState(version=1, active_alias=None, updated_at=None)


def test_state_store_round_trips_and_sets_private_permissions(tmp_path):
    paths = resolve_paths(tmp_path)
    store = StateStore(paths.state_file)
    state = AppState(version=1, active_alias="work-1", updated_at="2026-03-31T12:00:00Z")

    store.save(state)

    assert store.load() == state
    assert oct(paths.switch_root.stat().st_mode & 0o777) == "0o700"
    assert oct(paths.state_file.stat().st_mode & 0o777) == "0o600"
    assert paths.state_file.read_bytes() == (
        b'{\n'
        b'  "active_alias": "work-1",\n'
        b'  "updated_at": "2026-03-31T12:00:00Z",\n'
        b'  "version": 1\n'
        b'}\n'
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff",
        json.dumps([]).encode("utf-8"),
        json.dumps({"version": "1"}).encode("utf-8"),
        json.dumps({"active_alias": ["work-1"]}).encode("utf-8"),
        json.dumps({"updated_at": 123}).encode("utf-8"),
    ],
)
def test_state_store_rejects_corrupt_state_with_state_file_error(tmp_path, payload):
    paths = resolve_paths(tmp_path)
    paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    paths.state_file.write_bytes(payload)
    store = StateStore(paths.state_file)

    with pytest.raises(StateFileError, match="Could not parse .*"):
        store.load()


def test_state_store_rejects_syntactically_malformed_json_text(tmp_path):
    paths = resolve_paths(tmp_path)
    paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    paths.state_file.write_text("{not json")
    store = StateStore(paths.state_file)

    with pytest.raises(StateFileError, match=r"Could not parse .*: .*Expecting property name enclosed in double quotes"):
        store.load()


def test_state_store_reports_read_failures_separately(tmp_path, monkeypatch):
    paths = resolve_paths(tmp_path)
    paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    paths.state_file.write_text("{}")
    store = StateStore(paths.state_file)

    def failing_read_bytes(self):
        raise OSError(errno.EIO, "read failed")

    monkeypatch.setattr(Path, "read_bytes", failing_read_bytes)

    with pytest.raises(StateFileError, match=r"Could not read .*: .*read failed"):
        store.load()


def test_state_store_rejects_non_dict_json_with_specific_message(tmp_path):
    paths = resolve_paths(tmp_path)
    paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    paths.state_file.write_text("[]")
    store = StateStore(paths.state_file)

    with pytest.raises(StateFileError, match=r"Could not parse .*: expected a JSON object"):
        store.load()


@pytest.mark.parametrize(
    "payload, message",
    [
        (json.dumps({"version": "1"}), r"version must be an integer"),
        (json.dumps({"active_alias": ["work-1"]}), r"active_alias must be a string or null"),
        (json.dumps({"updated_at": 123}), r"updated_at must be a string or null"),
    ],
)
def test_state_store_reports_shape_errors_with_specific_messages(tmp_path, payload, message):
    paths = resolve_paths(tmp_path)
    paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    paths.state_file.write_text(payload)
    store = StateStore(paths.state_file)

    with pytest.raises(StateFileError, match=message):
        store.load()


def test_ensure_private_dir_applies_private_permissions_to_nested_paths(tmp_path):
    nested = tmp_path / "outer" / "inner"

    fs.ensure_private_dir(nested)

    assert nested.exists()
    assert oct((tmp_path / "outer").stat().st_mode & 0o777) == "0o700"
    assert oct(nested.stat().st_mode & 0o777) == "0o700"


def test_ensure_private_dir_with_root_secures_existing_ancestors(tmp_path):
    app_root = tmp_path / "app"
    permissive_parent = app_root / "cache"
    nested = permissive_parent / "state"
    permissive_parent.mkdir(parents=True)
    app_root.chmod(0o755)
    permissive_parent.chmod(0o755)

    fs.ensure_private_dir(nested, root=app_root)

    assert oct(app_root.stat().st_mode & 0o777) == "0o700"
    assert oct(permissive_parent.stat().st_mode & 0o777) == "0o700"
    assert oct(nested.stat().st_mode & 0o777) == "0o700"


def test_atomic_write_bytes_with_root_secures_existing_ancestor(tmp_path):
    app_root = tmp_path / "app"
    permissive_parent = app_root / "cache"
    target = permissive_parent / "state.json"
    permissive_parent.mkdir(parents=True)
    app_root.chmod(0o755)
    permissive_parent.chmod(0o755)

    fs.atomic_write_bytes(target, b"hello", root=app_root)

    assert oct(app_root.stat().st_mode & 0o777) == "0o700"
    assert oct(permissive_parent.stat().st_mode & 0o777) == "0o700"
    assert target.read_bytes() == b"hello"


def test_atomic_write_bytes_rejects_symlinked_path_segments(tmp_path):
    app_root = tmp_path / "app"
    outside_dir = tmp_path / "outside"
    escaped_dir = app_root / "cache"
    target = escaped_dir / "state.json"
    outside_dir.mkdir()
    app_root.mkdir()
    escaped_dir.symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        fs.atomic_write_bytes(target, b"hello", root=app_root)

    assert not (outside_dir / "state.json").exists()


def test_atomic_write_bytes_flushes_syncs_and_cleans_up_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "state.json"
    calls: list[str] = []
    original_fsync = os.fsync

    def recording_fsync(fd):
        calls.append("fsync")
        return original_fsync(fd)

    def failing_replace(source, destination):
        calls.append("replace")
        raise OSError("boom")

    monkeypatch.setattr(fs.os, "fsync", recording_fsync)
    monkeypatch.setattr(fs.os, "replace", failing_replace)

    with pytest.raises(OSError):
        fs.atomic_write_bytes(target, b"hello")

    assert list(target.parent.iterdir()) == []
    assert "replace" in calls
    assert calls.count("fsync") >= 1


def test_atomic_write_bytes_fsyncs_parent_directory_on_success(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "state.json"
    calls: list[str] = []
    original_fsync = os.fsync

    def recording_fsync(fd):
        calls.append("dir-fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file-fsync")
        return original_fsync(fd)

    monkeypatch.setattr(fs.os, "fsync", recording_fsync)

    fs.atomic_write_bytes(target, b"hello")

    assert target.read_bytes() == b"hello"
    assert calls.count("file-fsync") == 1
    assert calls.count("dir-fsync") == 1


def test_atomic_write_bytes_cleans_up_if_chmod_fails(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "state.json"
    original_chmod = fs.os.chmod
    calls = {"chmod": 0}

    def failing_second_chmod(path, mode):
        calls["chmod"] += 1
        if calls["chmod"] == 2:
            raise OSError(errno.EACCES, "chmod failed")
        return original_chmod(path, mode)

    monkeypatch.setattr(fs.os, "chmod", failing_second_chmod)

    with pytest.raises(OSError):
        fs.atomic_write_bytes(target, b"hello")

    assert list(target.parent.iterdir()) == []


def test_atomic_write_bytes_propagates_real_directory_fsync_failures(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "state.json"
    original_fsync = os.fsync

    def failing_dir_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "directory fsync failed")
        return original_fsync(fd)

    monkeypatch.setattr(fs.os, "fsync", failing_dir_fsync)

    with pytest.raises(OSError, match="directory fsync failed"):
        fs.atomic_write_bytes(target, b"hello")

    assert target.read_bytes() == b"hello"


def test_atomic_write_bytes_ignores_unsupported_directory_fsync(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "state.json"
    original_fsync = os.fsync

    def unsupported_dir_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "directory fsync unsupported")
        return original_fsync(fd)

    monkeypatch.setattr(fs.os, "fsync", unsupported_dir_fsync)

    fs.atomic_write_bytes(target, b"hello")

    assert target.read_bytes() == b"hello"


def test_atomic_copy_file_and_file_digest(tmp_path):
    source = tmp_path / "source.json"
    target = tmp_path / "nested" / "copy.json"
    source.write_bytes(b"hello")

    fs.atomic_copy_file(source, target, root=tmp_path)

    assert target.read_bytes() == b"hello"
    assert fs.file_digest(target) == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert fs.file_digest(tmp_path / "missing.json") is None
