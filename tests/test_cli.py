from types import SimpleNamespace

import pytest

from codex_switch.automation_models import RateLimitSnapshot, RateLimitWindow, UsageSource
from codex_switch.automation_db import SwitchEventRecord
from codex_switch.cli import build_default_manager
from codex_switch.cli import build_parser
from codex_switch.cli import format_alias_lines
from codex_switch.cli import format_auto_history_lines
from codex_switch.cli import format_auto_source_lines
from codex_switch.cli import format_auto_status_lines
from codex_switch.cli import format_daemon_status_lines
from codex_switch.cli import format_status_lines
from codex_switch.cli import main
from codex_switch.errors import CodexSwitchError
from codex_switch.errors import AutomationSourceUnavailableError
from codex_switch.models import (
    AliasListEntry,
    AutoSourceResult,
    AutoStatusResult,
    AppConfig,
    AppState,
    DaemonStatusResult,
    ListFormat,
    LoginMode,
    StatusResult,
)
from codex_switch.paths import resolve_paths


class ProbeStateStore:
    def __init__(self, _path, *, active_alias: str | None = "beta") -> None:
        self._active_alias = active_alias

    def load(self) -> AppState:
        return AppState(active_alias=self._active_alias, updated_at="2026-04-06T00:00:00Z")


class ProbeAccounts:
    def __init__(self, _path) -> None:
        self.read_snapshot_calls: list[str] = []

    def read_snapshot(self, alias: str) -> bytes:
        self.read_snapshot_calls.append(alias)
        return f'{{"alias":"{alias}"}}'.encode()


def test_build_parser_registers_expected_subcommands():
    parser = build_parser()
    subparsers = next(action for action in parser._actions if getattr(action, "choices", None))
    assert set(subparsers.choices) == {
        "add",
        "use",
        "list",
        "remove",
        "status",
        "daemon",
        "auto",
    }


def test_build_parser_add_includes_alias_argument():
    parser = build_parser()

    namespace = parser.parse_args(["add", "work"])

    assert namespace.command == "add"
    assert namespace.alias == "work"


def test_build_parser_add_accepts_device_auth_flag():
    parser = build_parser()

    namespace = parser.parse_args(["add", "work", "--device-auth"])

    assert namespace.command == "add"
    assert namespace.alias == "work"
    assert namespace.device_auth is True


def test_build_parser_add_accepts_isolated_flag():
    parser = build_parser()

    namespace = parser.parse_args(["add", "work", "--isolated", "--device-auth"])

    assert namespace.command == "add"
    assert namespace.alias == "work"
    assert namespace.isolated is True
    assert namespace.device_auth is True


def test_build_parser_list_accepts_refresh_flag():
    parser = build_parser()

    namespace = parser.parse_args(["list", "--refresh"])

    assert namespace.command == "list"
    assert namespace.refresh is True


def test_build_default_manager_threads_login_mode_through_runner(monkeypatch):
    captured: dict[str, object] = {}

    class FakeManager:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("codex_switch.cli.CodexSwitchManager", FakeManager)
    monkeypatch.setattr(
        "codex_switch.cli.resolve_paths",
        lambda: SimpleNamespace(accounts_dir=object(), state_file=object()),
    )
    monkeypatch.setattr("codex_switch.cli.AccountStore", lambda _path: object())
    monkeypatch.setattr("codex_switch.cli.StateStore", lambda _path: object())
    monkeypatch.setattr("codex_switch.process_guard.ensure_codex_not_running", lambda: None)
    monkeypatch.setattr(
        "codex_switch.codex_login.run_codex_login",
        lambda login_mode=LoginMode.BROWSER: captured.setdefault("login_mode", login_mode),
    )

    build_default_manager()

    login_runner = captured["login_runner"]
    assert callable(login_runner)
    login_runner(LoginMode.DEVICE_AUTH)
    assert captured["login_mode"] == LoginMode.DEVICE_AUTH
    assert callable(captured["alias_metadata_probe"])


def test_build_default_manager_uses_fresh_rpc_source_per_alias_probe(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    rpc_instances: list[int] = []
    accounts = ProbeAccounts(object())

    class FakeManager:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class FakeRpcSource:
        def __init__(self, *args, **kwargs):
            rpc_instances.append(len(rpc_instances) + 1)
            self.instance_id = rpc_instances[-1]

        def poll(self, *, active_alias: str):
            return SimpleNamespace(
                account_identity=SimpleNamespace(
                    email=f"{active_alias}@example.com",
                    plan_type=f"plan-{self.instance_id}",
                    fingerprint=f"fp-{self.instance_id}",
                    observed_at=f"2026-04-05T00:0{self.instance_id}:00Z",
                ),
                rate_limits=[],
            )

    class FakePtySource:
        def probe(self, *, alias: str, observed_at: str):
            raise AssertionError("PTY fallback should not be used")

    monkeypatch.setattr("codex_switch.cli.CodexSwitchManager", FakeManager)
    monkeypatch.setattr("codex_switch.cli.resolve_paths", lambda: resolve_paths(tmp_path))
    monkeypatch.setattr("codex_switch.cli.AccountStore", lambda _path: accounts)
    monkeypatch.setattr("codex_switch.cli.StateStore", lambda _path: ProbeStateStore(_path))
    monkeypatch.setattr("codex_switch.process_guard.ensure_codex_not_running", lambda: None)
    monkeypatch.setattr("codex_switch.codex_login.run_codex_login", lambda _login_mode=LoginMode.BROWSER: None)
    monkeypatch.setattr("codex_switch.daemon_runtime.AppServerRpcSource", FakeRpcSource)
    monkeypatch.setattr("codex_switch.daemon_runtime.CodexCliPtySource", FakePtySource)

    build_default_manager()

    probe = captured["alias_metadata_probe"]
    first = probe("alpha")
    second = probe("beta")

    assert rpc_instances == [1, 2]
    assert first.account_plan_type == "plan-1"
    assert second.account_plan_type == "plan-2"


def test_build_default_manager_probe_returns_rate_limits_without_account_identity(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    snapshots = [
        RateLimitSnapshot(
            alias="alpha",
            limit_id="codex",
            limit_name="codex",
            observed_via=UsageSource.RPC,
            plan_type=None,
            primary_window=RateLimitWindow(used_percent=58, resets_at="2026-04-06T05:00:00Z", window_duration_mins=300),
            secondary_window=RateLimitWindow(used_percent=29, resets_at="2026-04-10T00:00:00Z", window_duration_mins=10080),
            credits_has_credits=None,
            credits_unlimited=None,
            credits_balance=None,
            observed_at="2026-04-06T00:00:00Z",
        )
    ]
    accounts = ProbeAccounts(object())

    class FakeManager:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class FakeRpcSource:
        def __init__(self, *args, **kwargs):
            pass

        def poll(self, *, active_alias: str):
            return SimpleNamespace(account_identity=None, rate_limits=snapshots)

    class FakePtySource:
        def probe(self, *, alias: str, observed_at: str):
            raise AssertionError("PTY fallback should not be used")

    monkeypatch.setattr("codex_switch.cli.CodexSwitchManager", FakeManager)
    monkeypatch.setattr("codex_switch.cli.resolve_paths", lambda: resolve_paths(tmp_path))
    monkeypatch.setattr("codex_switch.cli.AccountStore", lambda _path: accounts)
    monkeypatch.setattr("codex_switch.cli.StateStore", lambda _path: ProbeStateStore(_path))
    monkeypatch.setattr("codex_switch.process_guard.ensure_codex_not_running", lambda: None)
    monkeypatch.setattr("codex_switch.codex_login.run_codex_login", lambda _login_mode=LoginMode.BROWSER: None)
    monkeypatch.setattr("codex_switch.daemon_runtime.AppServerRpcSource", FakeRpcSource)
    monkeypatch.setattr("codex_switch.daemon_runtime.CodexCliPtySource", FakePtySource)

    build_default_manager()

    observation = captured["alias_metadata_probe"]("alpha")

    assert observation is not None
    assert observation.account_plan_type is None
    assert observation.rate_limits == tuple(snapshots)


def test_build_default_manager_probe_wraps_pty_snapshot_in_rate_limits(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    snapshot = RateLimitSnapshot(
        alias="alpha",
        limit_id="codex",
        limit_name="codex",
        observed_via=UsageSource.PTY,
        plan_type="plus",
        primary_window=RateLimitWindow(used_percent=34, resets_at="2026-04-06T05:00:00Z", window_duration_mins=300),
        secondary_window=RateLimitWindow(used_percent=67, resets_at="2026-04-10T00:00:00Z", window_duration_mins=10080),
        credits_has_credits=None,
        credits_unlimited=None,
        credits_balance=None,
        observed_at="2026-04-06T00:12:00Z",
        )

    class FakeManager:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class FakeRpcSource:
        def __init__(self, *args, **kwargs):
            pass

        def poll(self, *, active_alias: str):
            raise AutomationSourceUnavailableError("rpc unavailable")

    class FakePtySource:
        def __init__(self, *args, **kwargs):
            pass

        def probe(self, *, alias: str, observed_at: str):
            return snapshot

    monkeypatch.setattr("codex_switch.cli.CodexSwitchManager", FakeManager)
    monkeypatch.setattr("codex_switch.cli.resolve_paths", lambda: resolve_paths(tmp_path))
    monkeypatch.setattr("codex_switch.cli.AccountStore", lambda _path: ProbeAccounts(_path))
    monkeypatch.setattr("codex_switch.cli.StateStore", lambda _path: ProbeStateStore(_path))
    monkeypatch.setattr("codex_switch.process_guard.ensure_codex_not_running", lambda: None)
    monkeypatch.setattr("codex_switch.codex_login.run_codex_login", lambda _login_mode=LoginMode.BROWSER: None)
    monkeypatch.setattr("codex_switch.daemon_runtime.AppServerRpcSource", FakeRpcSource)
    monkeypatch.setattr("codex_switch.daemon_runtime.CodexCliPtySource", FakePtySource)

    build_default_manager()

    observation = captured["alias_metadata_probe"]("alpha")

    assert observation is not None
    assert observation.account_plan_type == "plus"
    assert observation.rate_limits == (snapshot,)


def test_build_default_manager_probe_reads_requested_alias_snapshot(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    read_snapshot_calls: list[str] = []

    class FakeManager:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class FakeAccounts:
        def __init__(self, _path):
            pass

        def read_snapshot(self, alias: str) -> bytes:
            read_snapshot_calls.append(alias)
            return f'{{"alias":"{alias}"}}'.encode()

    class FakeRpcSource:
        def __init__(self, *args, **kwargs):
            pass

        def poll(self, *, active_alias: str):
            return SimpleNamespace(
                account_identity=SimpleNamespace(
                    email=f"{active_alias}@example.com",
                    plan_type="team",
                    fingerprint=f"fp-{active_alias}",
                    observed_at="2026-04-06T00:00:00Z",
                ),
                rate_limits=[],
            )

    class FakePtySource:
        def probe(self, *, alias: str, observed_at: str):
            raise AssertionError("PTY fallback should not be used")

    monkeypatch.setattr("codex_switch.cli.CodexSwitchManager", FakeManager)
    monkeypatch.setattr("codex_switch.cli.resolve_paths", lambda: resolve_paths(tmp_path))
    monkeypatch.setattr("codex_switch.cli.AccountStore", FakeAccounts)
    monkeypatch.setattr("codex_switch.cli.StateStore", lambda _path: ProbeStateStore(_path))
    monkeypatch.setattr("codex_switch.process_guard.ensure_codex_not_running", lambda: None)
    monkeypatch.setattr("codex_switch.codex_login.run_codex_login", lambda _login_mode=LoginMode.BROWSER: None)
    monkeypatch.setattr("codex_switch.daemon_runtime.AppServerRpcSource", FakeRpcSource)
    monkeypatch.setattr("codex_switch.daemon_runtime.CodexCliPtySource", FakePtySource)

    build_default_manager()

    observation = captured["alias_metadata_probe"]("alpha")

    assert observation is not None
    assert read_snapshot_calls == ["alpha"]


def test_build_default_manager_probe_preserves_multiple_rpc_snapshots(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    snapshots = [
        RateLimitSnapshot(
            alias="alpha",
            limit_id="other",
            limit_name="other",
            observed_via=UsageSource.RPC,
            plan_type=None,
            primary_window=RateLimitWindow(used_percent=10, resets_at="2026-04-06T05:00:00Z", window_duration_mins=300),
            secondary_window=RateLimitWindow(used_percent=20, resets_at="2026-04-10T00:00:00Z", window_duration_mins=10080),
            credits_has_credits=None,
            credits_unlimited=None,
            credits_balance=None,
            observed_at="2026-04-06T00:00:00Z",
        ),
        RateLimitSnapshot(
            alias="alpha",
            limit_id="codex",
            limit_name="codex",
            observed_via=UsageSource.RPC,
            plan_type="plus",
            primary_window=RateLimitWindow(used_percent=58, resets_at="2026-04-06T05:00:00Z", window_duration_mins=300),
            secondary_window=RateLimitWindow(used_percent=29, resets_at="2026-04-10T00:00:00Z", window_duration_mins=10080),
            credits_has_credits=None,
            credits_unlimited=None,
            credits_balance=None,
            observed_at="2026-04-06T00:01:00Z",
        ),
    ]
    accounts = ProbeAccounts(object())

    class FakeManager:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class FakeRpcSource:
        def __init__(self, *args, **kwargs):
            pass

        def poll(self, *, active_alias: str):
            return SimpleNamespace(account_identity=None, rate_limits=snapshots)

    class FakePtySource:
        def probe(self, *, alias: str, observed_at: str):
            raise AssertionError("PTY fallback should not be used")

    monkeypatch.setattr("codex_switch.cli.CodexSwitchManager", FakeManager)
    monkeypatch.setattr("codex_switch.cli.resolve_paths", lambda: resolve_paths(tmp_path))
    monkeypatch.setattr("codex_switch.cli.AccountStore", lambda _path: accounts)
    monkeypatch.setattr("codex_switch.cli.StateStore", lambda _path: ProbeStateStore(_path))
    monkeypatch.setattr("codex_switch.process_guard.ensure_codex_not_running", lambda: None)
    monkeypatch.setattr("codex_switch.codex_login.run_codex_login", lambda _login_mode=LoginMode.BROWSER: None)
    monkeypatch.setattr("codex_switch.daemon_runtime.AppServerRpcSource", FakeRpcSource)
    monkeypatch.setattr("codex_switch.daemon_runtime.CodexCliPtySource", FakePtySource)

    build_default_manager()

    observation = captured["alias_metadata_probe"]("alpha")

    assert observation is not None
    assert observation.account_plan_type == "plus"
    assert observation.rate_limits == tuple(snapshots)


@pytest.mark.parametrize("command", ["add", "use", "remove"])
def test_build_parser_requires_alias_for_mutating_commands(command):
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([command])


def test_build_parser_list_takes_no_alias_argument():
    parser = build_parser()

    namespace = parser.parse_args(["list"])

    assert namespace.command == "list"
    assert not hasattr(namespace, "alias")


def test_build_parser_daemon_group_has_expected_subcommands():
    parser = build_parser()
    namespace = parser.parse_args(["daemon", "start"])

    assert namespace.command == "daemon"
    assert namespace.daemon_command == "start"


def test_build_parser_auto_history_accepts_limit():
    parser = build_parser()
    namespace = parser.parse_args(["auto", "history", "--limit", "7"])

    assert namespace.command == "auto"
    assert namespace.auto_command == "history"
    assert namespace.limit == 7


def test_format_alias_lines_marks_active_alias():
    assert format_alias_lines(
        [
            AliasListEntry(alias="personal", plan_type=None),
            AliasListEntry(alias="work", plan_type=None),
        ],
        "work",
    ) == [
        "  personal -- 5h left: ? -- weekly left: ?",
        "* work -- 5h left: ? -- weekly left: ?",
    ]


def test_format_alias_lines_handles_empty_alias_list():
    assert format_alias_lines([], None) == ["No aliases configured."]


def test_format_alias_lines_appends_plan_types_when_known():
    assert format_alias_lines(
        [
            AliasListEntry(alias="backup", plan_type=None),
            AliasListEntry(alias="beta", plan_type="plus"),
        ],
        "beta",
    ) == [
        "  backup -- 5h left: ? -- weekly left: ?",
        "* beta -- plus -- 5h left: ? -- weekly left: ?",
    ]


def test_format_alias_lines_omits_blank_plan_types():
    assert format_alias_lines(
        [AliasListEntry(alias="beta", plan_type="")],
        "beta",
    ) == ["* beta -- 5h left: ? -- weekly left: ?"]


def test_format_alias_lines_renders_labelled_usage_segments():
    assert format_alias_lines(
        [
            AliasListEntry(
                alias="backup",
                plan_type=None,
                five_hour_left_percent=None,
                weekly_left_percent=None,
            ),
            AliasListEntry(
                alias="beta",
                plan_type="plus",
                five_hour_left_percent=42,
                weekly_left_percent=71,
            ),
        ],
        "beta",
        ListFormat.LABELLED,
    ) == [
        "  backup -- 5h left: ? -- weekly left: ?",
        "* beta -- plus -- 5h left: 42% -- weekly left: 71%",
    ]


def test_format_alias_lines_renders_ascii_table():
    assert format_alias_lines(
        [
            AliasListEntry(
                alias="backup",
                plan_type=None,
                five_hour_left_percent=None,
                weekly_left_percent=None,
            ),
            AliasListEntry(
                alias="beta",
                plan_type="plus",
                five_hour_left_percent=42,
                weekly_left_percent=71,
            ),
        ],
        "beta",
        ListFormat.TABLE,
    ) == [
        "active  alias   type  5h left  weekly left",
        "------  ------  ----  -------  -----------",
        "        backup        ?        ?",
        "*       beta    plus  42%      71%",
    ]


def test_format_status_lines_marks_dirty_state():
    status = StatusResult(
        active_alias="work",
        snapshot_exists=True,
        live_auth_exists=True,
        in_sync=False,
    )

    assert format_status_lines(status) == [
        "active alias: work",
        "snapshot: present",
        "live auth: present",
        "sync: dirty",
    ]


def test_format_status_lines_with_no_active_alias_is_two_lines():
    status = StatusResult(
        active_alias=None,
        snapshot_exists=False,
        live_auth_exists=True,
        in_sync=None,
    )

    assert format_status_lines(status) == [
        "active alias: none",
        "live auth: present",
    ]


def test_format_daemon_status_lines_covers_running_and_stale_states():
    running = DaemonStatusResult(running=True, pid=123, pid_file_exists=True, stale_pid_file=False)
    stale = DaemonStatusResult(running=False, pid=456, pid_file_exists=True, stale_pid_file=True)

    assert format_daemon_status_lines(running) == [
        "daemon: running",
        "pid: 123",
    ]
    assert format_daemon_status_lines(stale) == [
        "daemon: stopped",
        "pid file: stale",
        "last pid: 456",
    ]


def test_format_auto_status_lines_handles_idle_and_active_states():
    idle = AutoStatusResult(
        active_alias=None,
        active_observed_via=None,
        active_observed_at=None,
        soft_switch_triggered=False,
        target_alias=None,
    )
    active = AutoStatusResult(
        active_alias="work",
        active_observed_via="RPC",
        active_observed_at="2026-04-05T00:00:00Z",
        soft_switch_triggered=True,
        target_alias="backup-a",
    )

    assert format_auto_status_lines(idle) == [
        "active alias: none",
        "automation: idle",
    ]
    assert format_auto_status_lines(active) == [
        "active alias: work",
        "telemetry: RPC @ 2026-04-05T00:00:00Z",
        "soft trigger: yes",
        "target alias: backup-a",
    ]


def test_format_auto_source_and_history_lines():
    source_rows = [
        AutoSourceResult(alias="work", observed_via="RPC", observed_at="2026-04-05T00:00:00Z"),
        AutoSourceResult(alias="backup", observed_via=None, observed_at=None),
    ]
    history_rows = [
        SwitchEventRecord(
            id=7,
            thread_id="t-1",
            from_alias="work",
            to_alias="backup",
            trigger_type="soft",
            trigger_limit_id=None,
            trigger_used_percent=95.0,
            requested_at="2026-04-05T01:00:00Z",
            switched_at="2026-04-05T01:00:05Z",
            resumed_at="2026-04-05T01:00:10Z",
            result="success",
            failure_message=None,
        )
    ]

    assert format_auto_source_lines(source_rows) == [
        "work: RPC @ 2026-04-05T00:00:00Z",
        "backup: telemetry missing",
    ]
    assert format_auto_history_lines(history_rows) == [
        "7 2026-04-05T01:00:00Z work->backup success",
    ]


def test_main_dispatches_add(monkeypatch, capsys):
    calls: list[tuple[str, str | None]] = []

    class FakeManager:
        def add(self, alias: str) -> None:
            calls.append(("add", alias))

    monkeypatch.setattr("codex_switch.cli.build_default_manager", lambda: FakeManager())

    result = main(["add", "work"])

    captured = capsys.readouterr()
    assert result == 0
    assert calls == [("add", "work")]
    assert captured.out == "added alias: work\n"


def test_main_dispatches_add_device_auth(monkeypatch, capsys):
    calls: list[tuple[str, str, LoginMode]] = []

    class FakeManager:
        def add(self, alias: str, login_mode: LoginMode = LoginMode.BROWSER) -> None:
            calls.append(("add", alias, login_mode))

    monkeypatch.setattr("codex_switch.cli.build_default_manager", lambda: FakeManager())

    result = main(["add", "work", "--device-auth"])

    captured = capsys.readouterr()
    assert result == 0
    assert calls == [("add", "work", LoginMode.DEVICE_AUTH)]
    assert captured.out == "added alias: work\n"


def test_main_dispatches_add_isolated_device_auth(monkeypatch, capsys):
    calls: list[tuple[str, str, LoginMode, bool]] = []

    class FakeManager:
        def add(
            self,
            alias: str,
            login_mode: LoginMode = LoginMode.BROWSER,
            *,
            isolated: bool = False,
        ) -> None:
            calls.append(("add", alias, login_mode, isolated))

    monkeypatch.setattr("codex_switch.cli.build_default_manager", lambda: FakeManager())

    result = main(["add", "work", "--isolated", "--device-auth"])

    captured = capsys.readouterr()
    assert result == 0
    assert calls == [("add", "work", LoginMode.DEVICE_AUTH, True)]
    assert captured.out == "added alias: work\n"


def test_main_dispatches_use(monkeypatch, capsys):
    calls: list[tuple[str, str | None]] = []

    class FakeManager:
        def use(self, alias: str) -> None:
            calls.append(("use", alias))

    monkeypatch.setattr("codex_switch.cli.build_default_manager", lambda: FakeManager())

    result = main(["use", "work"])

    captured = capsys.readouterr()
    assert result == 0
    assert calls == [("use", "work")]
    assert captured.out == "active alias: work\n"


def test_main_dispatches_list(monkeypatch, capsys):
    refresh_calls: list[bool] = []

    class FakeManager:
        def list_aliases(self, *, refresh: bool = True) -> tuple[list[AliasListEntry], str | None]:
            refresh_calls.append(refresh)
            return [
                AliasListEntry(alias="personal", plan_type=None),
                AliasListEntry(alias="work", plan_type="pro"),
            ], "work"

    monkeypatch.setattr("codex_switch.cli.build_default_manager", lambda: FakeManager())
    monkeypatch.setattr(
        "codex_switch.cli.load_app_config",
        lambda _path: AppConfig(list_format=ListFormat.LABELLED),
    )
    monkeypatch.setattr(
        "codex_switch.cli.resolve_paths",
        lambda: SimpleNamespace(config_file=object()),
    )

    result = main(["list"])

    captured = capsys.readouterr()
    assert result == 0
    assert refresh_calls == [False]
    assert captured.out.splitlines() == [
        "  personal -- 5h left: ? -- weekly left: ?",
        "* work -- pro -- 5h left: ? -- weekly left: ?",
    ]


def test_main_dispatches_list_refresh(monkeypatch, capsys):
    refresh_calls: list[bool] = []

    class FakeManager:
        def list_aliases(self, *, refresh: bool = True) -> tuple[list[AliasListEntry], str | None]:
            refresh_calls.append(refresh)
            return [AliasListEntry(alias="work", plan_type="pro")], "work"

    monkeypatch.setattr("codex_switch.cli.build_default_manager", lambda: FakeManager())
    monkeypatch.setattr(
        "codex_switch.cli.load_app_config",
        lambda _path: AppConfig(list_format=ListFormat.LABELLED),
    )
    monkeypatch.setattr(
        "codex_switch.cli.resolve_paths",
        lambda: SimpleNamespace(config_file=object()),
    )

    result = main(["list", "--refresh"])

    captured = capsys.readouterr()
    assert result == 0
    assert refresh_calls == [True]
    assert captured.out.splitlines() == [
        "* work -- pro -- 5h left: ? -- weekly left: ?",
    ]


def test_main_list_uses_configured_table_format(monkeypatch, capsys):
    class FakeManager:
        def list_aliases(self, *, refresh: bool = True):
            return (
                [
                    AliasListEntry(
                        alias="beta",
                        plan_type="plus",
                        five_hour_left_percent=42,
                        weekly_left_percent=71,
                    )
                ],
                "beta",
            )

    monkeypatch.setattr("codex_switch.cli.build_default_manager", lambda: FakeManager())
    monkeypatch.setattr(
        "codex_switch.cli.load_app_config",
        lambda _path: AppConfig(list_format=ListFormat.TABLE),
    )
    monkeypatch.setattr(
        "codex_switch.cli.resolve_paths",
        lambda: SimpleNamespace(config_file=object()),
    )

    assert main(["list"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "active  alias  type  5h left  weekly left",
        "------  -----  ----  -------  -----------",
        "*       beta   plus  42%      71%",
    ]


def test_main_dispatches_remove(monkeypatch, capsys):
    calls: list[tuple[str, str | None]] = []

    class FakeManager:
        def remove(self, alias: str) -> None:
            calls.append(("remove", alias))

    monkeypatch.setattr("codex_switch.cli.build_default_manager", lambda: FakeManager())

    result = main(["remove", "work"])

    captured = capsys.readouterr()
    assert result == 0
    assert calls == [("remove", "work")]
    assert captured.out == "removed alias: work\n"


def test_main_dispatches_status(monkeypatch, capsys):
    class FakeManager:
        def status(self) -> StatusResult:
            return StatusResult(
                active_alias=None,
                snapshot_exists=False,
                live_auth_exists=True,
                in_sync=None,
            )

    monkeypatch.setattr("codex_switch.cli.build_default_manager", lambda: FakeManager())

    result = main(["status"])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out.splitlines() == [
        "active alias: none",
        "live auth: present",
    ]


def test_main_dispatches_daemon_commands(monkeypatch, capsys):
    calls: list[str] = []

    class FakeManager:
        def daemon_install(self) -> None:
            calls.append("install")
            return None

        def daemon_start(self) -> DaemonStatusResult:
            calls.append("start")
            return DaemonStatusResult(
                running=True,
                pid=200,
                pid_file_exists=True,
                stale_pid_file=False,
            )

        def daemon_stop(self) -> DaemonStatusResult:
            calls.append("stop")
            return DaemonStatusResult(
                running=False,
                pid=None,
                pid_file_exists=False,
                stale_pid_file=False,
            )

        def daemon_status(self) -> DaemonStatusResult:
            calls.append("status")
            return DaemonStatusResult(
                running=False,
                pid=300,
                pid_file_exists=True,
                stale_pid_file=True,
            )

        def daemon_enable(self) -> DaemonStatusResult:
            calls.append("enable")
            return DaemonStatusResult(
                running=True,
                pid=400,
                pid_file_exists=True,
                stale_pid_file=False,
            )

        def daemon_disable(self) -> DaemonStatusResult:
            calls.append("disable")
            return DaemonStatusResult(
                running=False,
                pid=None,
                pid_file_exists=False,
                stale_pid_file=False,
            )

    monkeypatch.setattr("codex_switch.cli.build_default_manager", lambda: FakeManager())

    assert main(["daemon", "install"]) == 0
    assert capsys.readouterr().out.strip() == "daemon installed"

    assert main(["daemon", "start"]) == 0
    assert capsys.readouterr().out.splitlines() == ["daemon: running", "pid: 200"]

    assert main(["daemon", "stop"]) == 0
    assert capsys.readouterr().out.splitlines() == ["daemon: stopped", "pid file: missing"]

    assert main(["daemon", "status"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "daemon: stopped",
        "pid file: stale",
        "last pid: 300",
    ]

    assert main(["daemon", "enable"]) == 0
    assert capsys.readouterr().out.splitlines() == ["daemon: running", "pid: 400"]

    assert main(["daemon", "disable"]) == 0
    assert capsys.readouterr().out.splitlines() == ["daemon: stopped", "pid file: missing"]
    assert calls == ["install", "start", "stop", "status", "enable", "disable"]


def test_main_dispatches_auto_commands(monkeypatch, capsys):
    class FakeManager:
        def auto_status(self) -> AutoStatusResult:
            return AutoStatusResult(
                active_alias="work",
                active_observed_via="RPC",
                active_observed_at="2026-04-05T00:00:00Z",
                soft_switch_triggered=True,
                target_alias="backup-a",
            )

        def auto_source(self) -> list[AutoSourceResult]:
            return [AutoSourceResult(alias="work", observed_via=None, observed_at=None)]

        def auto_history(self, limit: int = 20) -> list[SwitchEventRecord]:
            assert limit == 5
            return [
                SwitchEventRecord(
                    id=1,
                    thread_id="t1",
                    from_alias="work",
                    to_alias="backup",
                    trigger_type="soft",
                    trigger_limit_id=None,
                    trigger_used_percent=95.0,
                    requested_at="2026-04-05T01:00:00Z",
                    switched_at=None,
                    resumed_at=None,
                    result="queued",
                    failure_message=None,
                )
            ]

        def auto_retry_resume(self) -> str:
            return "thread-123"

    monkeypatch.setattr("codex_switch.cli.build_default_manager", lambda: FakeManager())

    assert main(["auto", "status"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "active alias: work",
        "telemetry: RPC @ 2026-04-05T00:00:00Z",
        "soft trigger: yes",
        "target alias: backup-a",
    ]

    assert main(["auto", "source"]) == 0
    assert capsys.readouterr().out.splitlines() == ["work: telemetry missing"]

    assert main(["auto", "history", "--limit", "5"]) == 0
    assert capsys.readouterr().out.splitlines() == ["1 2026-04-05T01:00:00Z work->backup queued"]

    assert main(["auto", "retry-resume"]) == 0
    assert capsys.readouterr().out.strip() == "resumed thread: thread-123"


def test_main_exits_via_parser_for_user_facing_errors(monkeypatch):
    class FakeManager:
        def add(self, alias: str) -> None:
            raise CodexSwitchError(f"bad alias: {alias}")

    monkeypatch.setattr("codex_switch.cli.build_default_manager", lambda: FakeManager())

    with pytest.raises(SystemExit) as exc_info:
        main(["add", "broken"])

    assert exc_info.value.code == 1
