from __future__ import annotations

import pytest

from codex_switch.accounts import AccountStore
from codex_switch.automation_db import AutomationStore
from codex_switch.automation_models import HandoffPhase, RateLimitSnapshot, RateLimitWindow, UsageSource
from codex_switch.errors import AutomationHandoffError
from codex_switch.manager import CodexSwitchManager
from codex_switch.models import AppState, DaemonStatusResult
from codex_switch.paths import resolve_paths
from codex_switch.state import StateStore


class DaemonSpy:
    def __init__(self) -> None:
        self.installs = 0
        self.starts = 0
        self.stops = 0
        self.status_checks = 0

    def install(self) -> None:
        self.installs += 1

    def start(self) -> DaemonStatusResult:
        self.starts += 1
        return DaemonStatusResult(running=True, pid=101, pid_file_exists=True, stale_pid_file=False)

    def stop(self) -> DaemonStatusResult:
        self.stops += 1
        return DaemonStatusResult(running=False, pid=None, pid_file_exists=False, stale_pid_file=False)

    def status(self) -> DaemonStatusResult:
        self.status_checks += 1
        return DaemonStatusResult(running=False, pid=None, pid_file_exists=False, stale_pid_file=False)


def make_snapshot(alias: str, primary_used: float | None, secondary_used: float | None) -> RateLimitSnapshot:
    return RateLimitSnapshot(
        alias=alias,
        limit_id=None,
        limit_name="Primary",
        observed_via=UsageSource.RPC,
        plan_type="pro",
        primary_window=RateLimitWindow(
            used_percent=primary_used,
            resets_at="2026-04-05T00:00:00Z",
            window_duration_mins=300,
        ),
        secondary_window=RateLimitWindow(
            used_percent=secondary_used,
            resets_at="2026-04-06T00:00:00Z",
            window_duration_mins=10080,
        ),
        credits_has_credits=True,
        credits_unlimited=False,
        credits_balance="10.0",
        observed_at="2026-04-05T00:00:00Z",
    )


def make_manager(tmp_path):
    paths = resolve_paths(tmp_path)
    accounts = AccountStore(paths.accounts_dir)
    state = StateStore(paths.state_file)
    automation = AutomationStore(paths.automation_db_file)
    daemon = DaemonSpy()
    resume_calls: list[str] = []
    manager = CodexSwitchManager(
        paths=paths,
        accounts=accounts,
        state=state,
        ensure_safe_to_mutate=lambda: None,
        login_runner=lambda _mode: None,
        automation=automation,
        daemon_controller=daemon,
        soft_switch_threshold=95.0,
        resume_runner=lambda thread_id: resume_calls.append(thread_id),
    )
    return manager, paths, accounts, state, automation, daemon, resume_calls


def test_daemon_start_initializes_store_and_starts_daemon(tmp_path):
    manager, paths, _accounts, _state, _automation, daemon, _resume_calls = make_manager(tmp_path)

    status = manager.daemon_start()

    assert daemon.starts == 1
    assert status.running is True
    assert paths.automation_db_file.exists()


def test_auto_status_returns_idle_when_no_active_alias(tmp_path):
    manager, _paths, _accounts, _state, _automation, _daemon, _resume_calls = make_manager(tmp_path)

    status = manager.auto_status()

    assert status.active_alias is None
    assert status.soft_switch_triggered is False
    assert status.target_alias is None


def test_auto_status_suggests_target_alias_after_soft_trigger(tmp_path):
    manager, _paths, accounts, state, automation, _daemon, _resume_calls = make_manager(tmp_path)
    accounts.write_snapshot_from_bytes("work", b"{}")
    accounts.write_snapshot_from_bytes("backup-a", b"{}")
    accounts.write_snapshot_from_bytes("backup-b", b"{}")
    state.save(AppState(active_alias="work", updated_at="2026-04-05T00:00:00Z"))

    automation.upsert_rate_limit(make_snapshot("work", 95, 20))
    automation.upsert_rate_limit(make_snapshot("backup-a", 15, 10))
    automation.upsert_rate_limit(make_snapshot("backup-b", 20, 15))

    status = manager.auto_status()

    assert status.active_alias == "work"
    assert status.soft_switch_triggered is True
    assert status.target_alias == "backup-a"
    assert status.active_observed_via == "RPC"
    assert status.active_observed_at == "2026-04-05T00:00:00Z"


def test_auto_source_lists_aliases_with_or_without_telemetry(tmp_path):
    manager, _paths, accounts, _state, automation, _daemon, _resume_calls = make_manager(tmp_path)
    accounts.write_snapshot_from_bytes("with-telemetry", b"{}")
    accounts.write_snapshot_from_bytes("without-telemetry", b"{}")
    automation.upsert_rate_limit(make_snapshot("with-telemetry", 10, 5))

    rows = manager.auto_source()

    assert rows[0].alias == "with-telemetry"
    assert rows[0].observed_via == "RPC"
    assert rows[1].alias == "without-telemetry"
    assert rows[1].observed_via is None


def test_auto_history_returns_latest_events(tmp_path):
    manager, _paths, _accounts, _state, automation, _daemon, _resume_calls = make_manager(tmp_path)
    automation.append_switch_event(
        thread_id="t1",
        from_alias="work",
        to_alias="backup-a",
        trigger_type="soft",
        trigger_limit_id=None,
        trigger_used_percent=95.0,
        requested_at="2026-04-05T01:00:00Z",
        switched_at="2026-04-05T01:00:05Z",
        resumed_at="2026-04-05T01:00:10Z",
        result="success",
        failure_message=None,
    )
    automation.append_switch_event(
        thread_id="t2",
        from_alias="backup-a",
        to_alias="backup-b",
        trigger_type="hard",
        trigger_limit_id="weekly",
        trigger_used_percent=100.0,
        requested_at="2026-04-05T02:00:00Z",
        switched_at=None,
        resumed_at=None,
        result="failed_resume",
        failure_message="resume failed",
    )

    rows = manager.auto_history(limit=1)

    assert len(rows) == 1
    assert rows[0].thread_id == "t2"
    assert rows[0].result == "failed_resume"


def test_auto_retry_resume_runs_resume_and_clears_failed_resume_state(tmp_path):
    manager, _paths, _accounts, _state, automation, _daemon, resume_calls = make_manager(tmp_path)
    automation.set_handoff_state(
        thread_id="thread-42",
        source_alias="work",
        target_alias="backup-a",
        phase=HandoffPhase.failed_resume,
        reason="resume failed",
        updated_at="2026-04-05T03:00:00Z",
    )

    assert manager.auto_retry_resume() == "thread-42"
    assert resume_calls == ["thread-42"]
    assert automation.get_handoff_state() is None


def test_auto_retry_resume_rejects_non_failed_resume_state(tmp_path):
    manager, _paths, _accounts, _state, automation, _daemon, _resume_calls = make_manager(tmp_path)
    automation.set_handoff_state(
        thread_id="thread-42",
        source_alias="work",
        target_alias="backup-a",
        phase=HandoffPhase.pending_resume,
        reason="in progress",
        updated_at="2026-04-05T03:00:00Z",
    )

    with pytest.raises(AutomationHandoffError, match="failed_resume"):
        manager.auto_retry_resume()
