import json
import tempfile

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

    with pytest.raises(StateFileError):
        store.load()


def test_atomic_write_bytes_flushes_syncs_and_cleans_up_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "state.json"
    calls: list[tuple[str, object]] = []
    original_named_temporary_file = tempfile.NamedTemporaryFile
    original_fsync = fs.os.fsync

    class TempProxy:
        def __init__(self, handle):
            self._handle = handle
            self.flushed = False

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._handle.__exit__(exc_type, exc, tb)

        def write(self, data):
            return self._handle.write(data)

        def flush(self):
            self.flushed = True
            calls.append(("flush", self._handle.name))
            return self._handle.flush()

        def fileno(self):
            return self._handle.fileno()

        @property
        def name(self):
            return self._handle.name

    def wrapped_named_temporary_file(*args, **kwargs):
        return TempProxy(original_named_temporary_file(*args, **kwargs))

    def recording_fsync(fd):
        calls.append(("fsync", fd))
        return original_fsync(fd)

    def failing_replace(source, destination):
        calls.append(("replace", source, destination))
        assert any(entry[0] == "flush" for entry in calls)
        assert len([entry for entry in calls if entry[0] == "fsync"]) >= 1
        raise OSError("boom")

    monkeypatch.setattr(fs.tempfile, "NamedTemporaryFile", wrapped_named_temporary_file)
    monkeypatch.setattr(fs.os, "fsync", recording_fsync)
    monkeypatch.setattr(fs.os, "replace", failing_replace)

    with pytest.raises(OSError):
        fs.atomic_write_bytes(target, b"hello")

    assert list(target.parent.iterdir()) == []
    assert any(entry[0] == "replace" for entry in calls)
    assert len([entry for entry in calls if entry[0] == "fsync"]) >= 1


def test_atomic_write_bytes_fsyncs_parent_directory_on_success(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "state.json"
    calls: list[tuple[str, object]] = []
    original_named_temporary_file = tempfile.NamedTemporaryFile
    original_fsync = fs.os.fsync

    class TempProxy:
        def __init__(self, handle):
            self._handle = handle
            self.flushed = False

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._handle.__exit__(exc_type, exc, tb)

        def write(self, data):
            return self._handle.write(data)

        def flush(self):
            self.flushed = True
            calls.append(("flush", self._handle.name))
            return self._handle.flush()

        def fileno(self):
            return self._handle.fileno()

        @property
        def name(self):
            return self._handle.name

    def wrapped_named_temporary_file(*args, **kwargs):
        return TempProxy(original_named_temporary_file(*args, **kwargs))

    def recording_fsync(fd):
        calls.append(("fsync", fd))
        return original_fsync(fd)

    monkeypatch.setattr(fs.tempfile, "NamedTemporaryFile", wrapped_named_temporary_file)
    monkeypatch.setattr(fs.os, "fsync", recording_fsync)

    fs.atomic_write_bytes(target, b"hello")

    assert target.read_bytes() == b"hello"
    assert len([entry for entry in calls if entry[0] == "flush"]) == 1
    assert len([entry for entry in calls if entry[0] == "fsync"]) == 2
