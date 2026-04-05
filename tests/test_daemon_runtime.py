from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
import time

import pytest

from codex_switch.accounts import AccountStore
from codex_switch.automation_db import AutomationStore
from codex_switch.automation_models import (
    AccountIdentitySnapshot,
    HandoffPhase,
    RateLimitSnapshot,
    RateLimitWindow,
    ThreadRuntimeSnapshot,
    ThreadTurnUsageSnapshot,
    UsageSource,
)
from codex_switch.daemon_runtime import (
    AppServerRpcSource,
    CodexCliPtySource,
    DaemonRuntime,
    RpcPollResult,
    build_parser,
)
from codex_switch.errors import AutomationSourceUnavailableError
from codex_switch.manager import CodexSwitchManager
from codex_switch.models import AppState
from codex_switch.paths import resolve_paths
from codex_switch.state import StateStore


class FakeRpcSource:
    def __init__(self, results) -> None:
        self._results = list(results)
        self.calls: list[str | None] = []

    def poll(self, *, active_alias: str | None) -> RpcPollResult:
        self.calls.append(active_alias)
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakePtySource:
    def __init__(self, snapshots) -> None:
        self._snapshots = list(snapshots)
        self.calls: list[tuple[str, str]] = []

    def probe(self, *, alias: str, observed_at: str) -> RateLimitSnapshot | None:
        self.calls.append((alias, observed_at))
        if not self._snapshots:
            return None
        return self._snapshots.pop(0)


class FakeCodexController:
    def __init__(self, fail_resume: bool = False) -> None:
        self.stop_calls = 0
        self.resume_calls: list[str] = []
        self._fail_resume = fail_resume

    def stop(self) -> None:
        self.stop_calls += 1

    def resume(self, thread_id: str) -> None:
        self.resume_calls.append(thread_id)
        if self._fail_resume:
            raise RuntimeError("resume failed")


def make_rate_limit_snapshot(
    alias: str,
    primary_used: float | None,
    secondary_used: float | None,
    *,
    observed_via: UsageSource = UsageSource.RPC,
    observed_at: str = "2026-04-05T00:00:00Z",
) -> RateLimitSnapshot:
    return RateLimitSnapshot(
        alias=alias,
        limit_id=None,
        limit_name="5h limit",
        observed_via=observed_via,
        plan_type="pro",
        primary_window=RateLimitWindow(
            used_percent=primary_used,
            resets_at="2026-04-05T01:00:00Z",
            window_duration_mins=300,
        ),
        secondary_window=RateLimitWindow(
            used_percent=secondary_used,
            resets_at="2026-04-12T00:00:00Z",
            window_duration_mins=10080,
        ),
        credits_has_credits=True,
        credits_unlimited=False,
        credits_balance="5.25",
        observed_at=observed_at,
    )


def make_fresh_observed_at() -> str:
    return (
        (datetime.now(timezone.utc) - timedelta(minutes=1))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def make_runtime(tmp_path, *, rpc_results, pty_snapshots=(), fail_resume: bool = False):
    paths = resolve_paths(tmp_path)
    accounts = AccountStore(paths.accounts_dir)
    state = StateStore(paths.state_file)
    store = AutomationStore(paths.automation_db_file)
    manager = CodexSwitchManager(
        paths=paths,
        accounts=accounts,
        state=state,
        ensure_safe_to_mutate=lambda: None,
        login_runner=lambda _mode: None,
        automation=store,
        soft_switch_threshold=95.0,
    )
    rpc_source = FakeRpcSource(rpc_results)
    pty_source = FakePtySource(pty_snapshots)
    codex_controller = FakeCodexController(fail_resume=fail_resume)
    runtime = DaemonRuntime(
        store=store,
        manager=manager,
        rpc_source=rpc_source,
        pty_source=pty_source,
        codex_controller=codex_controller,
        can_mutate_auth=lambda: False,
        poll_interval_seconds=0.01,
    )
    return runtime, paths, accounts, state, store, rpc_source, pty_source, codex_controller


def test_daemon_runtime_parser_supports_run_command():
    parser = build_parser()

    namespace = parser.parse_args(["run", "--home", "/tmp/demo", "--poll-interval", "5"])

    assert namespace.command == "run"
    assert namespace.home == "/tmp/demo"
    assert namespace.poll_interval == 5.0


def test_daemon_runtime_run_once_persists_rpc_observations(tmp_path):
    runtime, _paths, accounts, state, store, rpc_source, _pty_source, _controller = make_runtime(
        tmp_path,
        rpc_results=[
            RpcPollResult(
                account_identity=AccountIdentitySnapshot(
                    email="work@example.com",
                    plan_type="pro",
                    fingerprint="fp-work",
                    observed_at="2026-04-05T00:00:00Z",
                ),
                rate_limits=[make_rate_limit_snapshot("work", 20, 10)],
                thread_runtime=ThreadRuntimeSnapshot(
                    thread_id="thread-1",
                    cwd="/repo",
                    model="gpt-5.4",
                    current_alias="work",
                    last_turn_id="turn-1",
                    last_known_status="running",
                    safe_to_switch=False,
                    last_total_tokens=120,
                    last_seen_at="2026-04-05T00:00:00Z",
                ),
                token_usage=[
                    ThreadTurnUsageSnapshot(
                        thread_id="thread-1",
                        turn_id="turn-1",
                        last_input_tokens=10,
                        last_cached_input_tokens=2,
                        last_output_tokens=5,
                        last_reasoning_output_tokens=1,
                        last_total_tokens=18,
                        total_input_tokens=10,
                        total_cached_input_tokens=2,
                        total_output_tokens=5,
                        total_reasoning_output_tokens=1,
                        total_tokens=18,
                        observed_at="2026-04-05T00:00:00Z",
                    )
                ],
                hard_limit_exceeded=False,
            )
        ],
    )
    accounts.write_snapshot_from_bytes("work", b'{"token":"work"}')
    state.save(AppState(active_alias="work", updated_at="2026-04-05T00:00:00Z"))

    runtime.run_once()

    assert rpc_source.calls == ["work"]
    assert store.list_aliases()[0].account_email == "work@example.com"
    assert store.latest_rate_limit_for_alias("work") is not None
    assert store.get_thread_runtime("thread-1") is not None
    assert store.list_thread_turn_usage(thread_id="thread-1")[0].turn_id == "turn-1"


def test_daemon_runtime_uses_pty_fallback_without_auto_switch_when_rpc_unavailable(tmp_path):
    runtime, _paths, accounts, state, store, _rpc_source, pty_source, controller = make_runtime(
        tmp_path,
        rpc_results=[AutomationSourceUnavailableError("rpc down")],
        pty_snapshots=[make_rate_limit_snapshot("work", 97, 80, observed_via=UsageSource.PTY)],
    )
    accounts.write_snapshot_from_bytes("work", b'{"token":"work"}')
    accounts.write_snapshot_from_bytes("backup", b'{"token":"backup"}')
    state.save(AppState(active_alias="work", updated_at="2026-04-05T00:00:00Z"))
    store.reconcile_aliases(["work", "backup"])
    store.upsert_rate_limit(make_rate_limit_snapshot("backup", 10, 5, observed_at="2026-04-05T00:00:00Z"))

    runtime.run_once()

    latest = store.latest_rate_limit_for_alias("work")
    assert latest is not None
    assert latest.observed_via == UsageSource.PTY
    assert pty_source.calls
    assert controller.stop_calls == 0
    assert controller.resume_calls == []


def test_daemon_runtime_soft_trigger_switches_at_safe_checkpoint(tmp_path):
    runtime, paths, accounts, state, store, _rpc_source, _pty_source, controller = make_runtime(
        tmp_path,
        rpc_results=[
            RpcPollResult(
                account_identity=None,
                rate_limits=[make_rate_limit_snapshot("work", 95, 10)],
                thread_runtime=ThreadRuntimeSnapshot(
                    thread_id="thread-1",
                    cwd="/repo",
                    model="gpt-5.4",
                    current_alias="work",
                    last_turn_id="turn-1",
                    last_known_status="idle",
                    safe_to_switch=True,
                    last_total_tokens=120,
                    last_seen_at="2026-04-05T00:00:00Z",
                ),
                token_usage=[],
                hard_limit_exceeded=False,
            )
        ],
    )
    accounts.write_snapshot_from_bytes("work", b'{"token":"work"}')
    accounts.write_snapshot_from_bytes("backup", b'{"token":"backup"}')
    state.save(AppState(active_alias="work", updated_at="2026-04-05T00:00:00Z"))
    store.reconcile_aliases(["work", "backup"])
    store.upsert_rate_limit(make_rate_limit_snapshot("backup", 10, 5, observed_at=make_fresh_observed_at()))
    paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.live_auth_file.write_bytes(b'{"token":"live-work"}')

    runtime.run_once()

    assert controller.stop_calls == 1
    assert controller.resume_calls == ["thread-1"]
    assert state.load().active_alias == "backup"
    assert store.get_handoff_state() is None
    assert store.list_switch_events(limit=1)[0].result == "success"


def test_daemon_runtime_defers_soft_trigger_until_thread_is_safe(tmp_path):
    runtime, paths, accounts, state, store, _rpc_source, _pty_source, controller = make_runtime(
        tmp_path,
        rpc_results=[
            RpcPollResult(
                account_identity=None,
                rate_limits=[make_rate_limit_snapshot("work", 95, 10)],
                thread_runtime=ThreadRuntimeSnapshot(
                    thread_id="thread-1",
                    cwd="/repo",
                    model="gpt-5.4",
                    current_alias="work",
                    last_turn_id="turn-1",
                    last_known_status="running",
                    safe_to_switch=False,
                    last_total_tokens=120,
                    last_seen_at="2026-04-05T00:00:00Z",
                ),
                token_usage=[],
                hard_limit_exceeded=False,
            ),
            RpcPollResult(
                account_identity=None,
                rate_limits=[make_rate_limit_snapshot("work", 96, 10, observed_at="2026-04-05T00:01:00Z")],
                thread_runtime=ThreadRuntimeSnapshot(
                    thread_id="thread-1",
                    cwd="/repo",
                    model="gpt-5.4",
                    current_alias="work",
                    last_turn_id="turn-2",
                    last_known_status="idle",
                    safe_to_switch=True,
                    last_total_tokens=140,
                    last_seen_at="2026-04-05T00:01:00Z",
                ),
                token_usage=[],
                hard_limit_exceeded=False,
            ),
        ],
    )
    accounts.write_snapshot_from_bytes("work", b'{"token":"work"}')
    accounts.write_snapshot_from_bytes("backup", b'{"token":"backup"}')
    state.save(AppState(active_alias="work", updated_at="2026-04-05T00:00:00Z"))
    store.reconcile_aliases(["work", "backup"])
    store.upsert_rate_limit(make_rate_limit_snapshot("backup", 10, 5, observed_at=make_fresh_observed_at()))
    paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.live_auth_file.write_bytes(b'{"token":"live-work"}')

    runtime.run_once()

    handoff = store.get_handoff_state()
    assert handoff is not None
    assert handoff.phase == HandoffPhase.pending_idle_checkpoint
    assert controller.stop_calls == 0

    runtime.run_once()

    assert controller.stop_calls == 1
    assert controller.resume_calls == ["thread-1"]
    assert store.get_handoff_state() is None
    assert state.load().active_alias == "backup"


def test_daemon_runtime_marks_failed_resume_and_keeps_target_active(tmp_path):
    runtime, paths, accounts, state, store, _rpc_source, _pty_source, controller = make_runtime(
        tmp_path,
        rpc_results=[
            RpcPollResult(
                account_identity=None,
                rate_limits=[make_rate_limit_snapshot("work", 95, 10)],
                thread_runtime=ThreadRuntimeSnapshot(
                    thread_id="thread-1",
                    cwd="/repo",
                    model="gpt-5.4",
                    current_alias="work",
                    last_turn_id="turn-1",
                    last_known_status="idle",
                    safe_to_switch=True,
                    last_total_tokens=120,
                    last_seen_at="2026-04-05T00:00:00Z",
                ),
                token_usage=[],
                hard_limit_exceeded=False,
            )
        ],
        fail_resume=True,
    )
    accounts.write_snapshot_from_bytes("work", b'{"token":"work"}')
    accounts.write_snapshot_from_bytes("backup", b'{"token":"backup"}')
    state.save(AppState(active_alias="work", updated_at="2026-04-05T00:00:00Z"))
    store.reconcile_aliases(["work", "backup"])
    store.upsert_rate_limit(make_rate_limit_snapshot("backup", 10, 5, observed_at=make_fresh_observed_at()))
    paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.live_auth_file.write_bytes(b'{"token":"live-work"}')

    runtime.run_once()

    handoff = store.get_handoff_state()
    assert handoff is not None
    assert handoff.phase == HandoffPhase.failed_resume
    assert controller.stop_calls == 1
    assert controller.resume_calls == ["thread-1"]
    assert state.load().active_alias == "backup"
    assert store.list_switch_events(limit=1)[0].result == "failed_resume"


def test_daemon_runtime_refreshes_stale_backup_aliases_when_auth_can_be_mutated(tmp_path):
    runtime, paths, accounts, state, store, rpc_source, _pty_source, _controller = make_runtime(
        tmp_path,
        rpc_results=[
            RpcPollResult(
                account_identity=None,
                rate_limits=[make_rate_limit_snapshot("backup", 15, 8)],
                thread_runtime=None,
                token_usage=[],
                hard_limit_exceeded=False,
            ),
            RpcPollResult(
                account_identity=None,
                rate_limits=[make_rate_limit_snapshot("work", 20, 10)],
                thread_runtime=ThreadRuntimeSnapshot(
                    thread_id="thread-1",
                    cwd="/repo",
                    model="gpt-5.4",
                    current_alias="work",
                    last_turn_id="turn-1",
                    last_known_status="running",
                    safe_to_switch=False,
                    last_total_tokens=120,
                    last_seen_at="2026-04-05T00:00:00Z",
                ),
                token_usage=[],
                hard_limit_exceeded=False,
            ),
        ],
    )
    runtime._can_mutate_auth = lambda: True
    accounts.write_snapshot_from_bytes("work", b'{"token":"work"}')
    accounts.write_snapshot_from_bytes("backup", b'{"token":"backup"}')
    state.save(AppState(active_alias="work", updated_at="2026-04-05T00:00:00Z"))
    paths.live_auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.live_auth_file.write_bytes(b'{"token":"live-work"}')

    runtime.run_once()

    assert rpc_source.calls == ["backup", "work"]
    backup = store.latest_rate_limit_for_alias("backup")
    assert backup is not None
    assert backup.primary_used_percent == 15
    assert state.load().active_alias == "work"


def test_daemon_runtime_run_forever_initializes_store_and_stops_on_request(tmp_path):
    runtime, _paths, accounts, state, _store, _rpc_source, _pty_source, _controller = make_runtime(
        tmp_path,
        rpc_results=[
            RpcPollResult(
                account_identity=None,
                rate_limits=[],
                thread_runtime=None,
                token_usage=[],
                hard_limit_exceeded=False,
            )
        ],
    )
    accounts.write_snapshot_from_bytes("work", b'{"token":"work"}')
    state.save(AppState(active_alias="work", updated_at="2026-04-05T00:00:00Z"))

    thread = threading.Thread(target=runtime.run_forever)
    thread.start()
    time.sleep(0.03)

    runtime.request_stop()
    thread.join(timeout=1.0)

    assert thread.is_alive() is False


def test_app_server_rpc_source_polls_requests_and_notifications():
    class FakeClient:
        def __init__(self) -> None:
            self.requests: list[tuple[int, str, object]] = []

        def send_request(self, request_id: int, method: str, params):
            self.requests.append((request_id, method, params))
            if method == "initialize":
                return type("Msg", (), {"payload": {"jsonrpc": "2.0", "id": request_id, "result": {"ok": True}}})()
            if method == "account/read":
                return type(
                    "Msg",
                    (),
                    {
                        "payload": {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": {
                                "account": {
                                    "email": "work@example.com",
                                    "planType": "pro",
                                    "fingerprint": "fp-work",
                                }
                            },
                        }
                    },
                )()
            if method == "account/rateLimits/read":
                return type(
                    "Msg",
                    (),
                    {
                        "payload": {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": {
                                "rateLimits": {
                                    "limitId": "codex",
                                    "limitName": None,
                                    "primary": {
                                        "usedPercent": 20,
                                        "resetsAt": 1_744_147_200,
                                        "windowDurationMins": 300,
                                    },
                                    "secondary": {
                                        "usedPercent": 10,
                                        "resetsAt": 1_744_752_000,
                                        "windowDurationMins": 10080,
                                    },
                                    "credits": {
                                        "hasCredits": True,
                                        "unlimited": False,
                                        "balance": "5.25",
                                    },
                                    "planType": "pro",
                                },
                            },
                        }
                    },
                )()
            raise AssertionError(f"unexpected method {method}")

        def drain_messages_nonblocking(self):
            return [
                type(
                    "Msg",
                    (),
                    {
                        "payload": {
                            "jsonrpc": "2.0",
                            "method": "thread/runtime/updated",
                            "params": {
                                "threadId": "thread-1",
                                "cwd": "/repo",
                                "model": "gpt-5.4",
                                "turnId": "turn-1",
                                "status": "usage_limit_exceeded",
                                "safeToSwitch": True,
                                "lastTotalTokens": 111,
                            },
                        }
                    },
                )(),
                type(
                    "Msg",
                    (),
                    {
                        "payload": {
                            "jsonrpc": "2.0",
                            "method": "thread/tokenUsage/updated",
                            "params": {
                                "threadId": "thread-1",
                                "turnId": "turn-1",
                                "lastUsage": {
                                    "inputTokens": 10,
                                    "cachedInputTokens": 2,
                                    "outputTokens": 5,
                                    "reasoningOutputTokens": 1,
                                    "totalTokens": 18,
                                },
                                "totalUsage": {
                                    "inputTokens": 10,
                                    "cachedInputTokens": 2,
                                    "outputTokens": 5,
                                    "reasoningOutputTokens": 1,
                                    "totalTokens": 18,
                                },
                            },
                        }
                    },
                )(),
            ]

    source = AppServerRpcSource(client_factory=lambda: FakeClient())

    result = source.poll(active_alias="work")

    assert source._client.requests == [
        (1, "initialize", {"clientInfo": {"name": "codex-switchd", "version": "0.1.0"}}),
        (2, "account/read", {}),
        (3, "account/rateLimits/read", {}),
    ]
    assert result.account_identity is not None
    assert result.account_identity.email == "work@example.com"
    assert result.rate_limits[0].alias == "work"
    assert result.rate_limits[0].limit_id == "codex"
    assert result.rate_limits[0].limit_name == "codex"
    assert result.thread_runtime is not None
    assert result.thread_runtime.thread_id == "thread-1"
    assert result.hard_limit_exceeded is True
    assert result.token_usage[0].turn_id == "turn-1"


def test_codex_cli_pty_source_probes_status_output(monkeypatch):
    class Completed:
        def __init__(self) -> None:
            self.stdout = "Credits: 12.50\n5h limit: 76% used, resets in 2h\nWeekly limit: 91% used, resets in 3d\n"
            self.stderr = ""

    monkeypatch.setattr("codex_switch.daemon_runtime.subprocess.run", lambda *args, **kwargs: Completed())

    source = CodexCliPtySource()

    snapshot = source.probe(alias="work", observed_at="2026-04-05T00:00:00Z")

    assert snapshot is not None
    assert snapshot.alias == "work"
    assert snapshot.observed_via == UsageSource.PTY
    assert snapshot.primary_window.used_percent == 76
    assert snapshot.secondary_window.used_percent == 91
    assert snapshot.credits_balance == "12.50"
