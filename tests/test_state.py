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
