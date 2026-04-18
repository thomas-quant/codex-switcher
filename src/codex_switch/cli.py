from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from codex_switch.automation_db import SwitchEventRecord
from codex_switch.accounts import AccountStore
from codex_switch.automation_rpc import CodexRpcClient
from codex_switch.config import load_app_config
from codex_switch.errors import CodexSwitchError
from codex_switch.isolated_codex import isolated_codex_env
from codex_switch.manager import CodexSwitchManager
from codex_switch.models import (
    AliasListEntry,
    AliasTelemetryObservation,
    AutoSourceResult,
    AutoStatusResult,
    DaemonStatusResult,
    ListFormat,
    LoginMode,
    StatusResult,
)
from codex_switch.paths import resolve_paths
from codex_switch.state import StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-switch")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("add", "use", "list", "remove", "status"):
        child = subparsers.add_parser(name)
        if name in {"add", "use", "remove"}:
            child.add_argument("alias")
        if name == "add":
            child.add_argument("--device-auth", action="store_true")
            child.add_argument("--isolated", action="store_true")
        if name == "list":
            child.add_argument("--refresh", action="store_true")
            child.add_argument("--email", action="store_true")
            format_group = child.add_mutually_exclusive_group()
            format_group.add_argument("--table", action="store_true")
            format_group.add_argument("--labelled", action="store_true")

    daemon_parser = subparsers.add_parser("daemon")
    daemon_subparsers = daemon_parser.add_subparsers(dest="daemon_command", required=True)
    for name in ("install", "start", "stop", "status", "enable", "disable"):
        daemon_subparsers.add_parser(name)

    auto_parser = subparsers.add_parser("auto")
    auto_subparsers = auto_parser.add_subparsers(dest="auto_command", required=True)
    auto_subparsers.add_parser("status")
    auto_subparsers.add_parser("source")
    history_parser = auto_subparsers.add_parser("history")
    history_parser.add_argument("--limit", type=int, default=20)
    auto_subparsers.add_parser("retry-resume")

    return parser


def build_default_manager() -> CodexSwitchManager:
    from codex_switch.codex_login import run_codex_login
    from codex_switch.process_guard import ensure_codex_not_running, is_codex_running

    paths = resolve_paths()
    accounts = AccountStore(paths.accounts_dir)
    state = StateStore(paths.state_file)

    def probe_alias_metadata(alias: str):
        auth_bytes = _load_probe_auth_bytes(alias=alias, accounts=accounts, paths=paths, state=state)
        observation, refreshed_auth_bytes = _probe_alias_metadata_from_auth_bytes_with_refreshed_auth(
            alias=alias,
            auth_bytes=auth_bytes,
        )
        if refreshed_auth_bytes is not None:
            accounts.write_snapshot_from_bytes(alias, refreshed_auth_bytes)
        return observation

    def identity_from_auth_bytes(auth_bytes: bytes) -> tuple[str | None, str | None] | None:
        observation = probe_alias_metadata_from_auth_bytes(alias="live", auth_bytes=auth_bytes)
        if observation is None:
            return None
        if observation.account_fingerprint is None and observation.account_email is None:
            return None
        return (
            observation.account_fingerprint,
            observation.account_email,
        )

    return CodexSwitchManager(
        paths=paths,
        accounts=accounts,
        state=state,
        ensure_safe_to_mutate=ensure_codex_not_running,
        is_codex_running=is_codex_running,
        login_runner=run_codex_login,
        alias_metadata_probe=probe_alias_metadata,
        identity_from_auth_bytes=identity_from_auth_bytes,
    )


def probe_alias_metadata_from_auth_bytes(
    *,
    alias: str,
    auth_bytes: bytes,
) -> AliasTelemetryObservation | None:
    observation, _refreshed_auth_bytes = _probe_alias_metadata_from_auth_bytes_with_refreshed_auth(
        alias=alias,
        auth_bytes=auth_bytes,
    )
    return observation


def _probe_alias_metadata_from_auth_bytes_with_refreshed_auth(
    *,
    alias: str,
    auth_bytes: bytes,
) -> tuple[AliasTelemetryObservation | None, bytes | None]:
    from codex_switch.daemon_runtime import AppServerRpcSource, CodexCliPtySource
    from codex_switch.errors import AutomationSourceUnavailableError
    from codex_switch.manager import utc_now

    with isolated_codex_env(auth_bytes) as env:
        auth_file = Path(env["CODEX_HOME"]) / "auth.json"
        rpc_source = AppServerRpcSource(
            client_factory=lambda: CodexRpcClient.launch_default(env=env)
        )
        try:
            poll = rpc_source.poll(active_alias=alias)
        except AutomationSourceUnavailableError:
            observed_at = utc_now()
            snapshot = CodexCliPtySource(env=env).probe(alias=alias, observed_at=observed_at)
            if snapshot is None:
                return None, _load_refreshed_auth_bytes(auth_file=auth_file, original_auth_bytes=auth_bytes)
            return AliasTelemetryObservation(
                account_email=None,
                account_plan_type=snapshot.plan_type,
                account_fingerprint=None,
                observed_at=snapshot.observed_at,
                rate_limits=(snapshot,),
            ), _load_refreshed_auth_bytes(auth_file=auth_file, original_auth_bytes=auth_bytes)
        finally:
            client = getattr(rpc_source, "_client", None)
            close = None if client is None else getattr(client, "close", None)
            if callable(close):
                close()

    account_identity = poll.account_identity
    plan_type = None if account_identity is None else account_identity.plan_type
    observed_at = None if account_identity is None else account_identity.observed_at
    if plan_type is None:
        for snapshot in poll.rate_limits:
            if snapshot.plan_type is not None:
                plan_type = snapshot.plan_type
                observed_at = snapshot.observed_at
                break
    if plan_type is None and account_identity is None and not poll.rate_limits:
        return None, _load_refreshed_auth_bytes(auth_file=auth_file, original_auth_bytes=auth_bytes)
    return AliasTelemetryObservation(
        account_email=None if account_identity is None else account_identity.email,
        account_plan_type=plan_type,
        account_fingerprint=None if account_identity is None else account_identity.fingerprint,
        observed_at=observed_at if observed_at is not None else utc_now(),
        rate_limits=tuple(poll.rate_limits),
    ), _load_refreshed_auth_bytes(auth_file=auth_file, original_auth_bytes=auth_bytes)


def _load_refreshed_auth_bytes(*, auth_file: Path, original_auth_bytes: bytes) -> bytes | None:
    if not auth_file.exists():
        return None
    refreshed_auth_bytes = auth_file.read_bytes()
    if refreshed_auth_bytes == original_auth_bytes:
        return None
    return refreshed_auth_bytes


def _load_probe_auth_bytes(
    *,
    alias: str,
    accounts: AccountStore,
    paths,
    state: StateStore,
) -> bytes:
    current = state.load()
    if alias == current.active_alias and paths.live_auth_file.exists():
        return paths.live_auth_file.read_bytes()
    return accounts.read_snapshot(alias)


def format_alias_lines(
    entries: list[AliasListEntry],
    active_alias: str | None,
    list_format: ListFormat = ListFormat.LABELLED,
    show_email: bool = False,
) -> list[str]:
    if not entries:
        return ["No aliases configured."]
    if list_format is ListFormat.TABLE:
        return format_alias_table_lines(entries, active_alias, show_email=show_email)
    return format_alias_labelled_lines(entries, active_alias, show_email=show_email)


def format_alias_labelled_lines(
    entries: list[AliasListEntry],
    active_alias: str | None,
    *,
    show_email: bool = False,
) -> list[str]:
    lines: list[str] = []
    for entry in entries:
        prefix = "* " if entry.alias == active_alias else "  "
        plan_type = entry.plan_type.strip() if entry.plan_type is not None else None
        segments = [entry.alias]
        if plan_type:
            segments.append(plan_type)
        if show_email:
            segments.append(f"email: {_format_email(entry.account_email)}")
        segments.append(f"5h left: {_format_percent(entry.five_hour_left_percent)}")
        segments.append(f"weekly left: {_format_percent(entry.weekly_left_percent)}")
        lines.append(f"{prefix}{' -- '.join(segments)}")
    return lines


def format_alias_table_lines(
    entries: list[AliasListEntry],
    active_alias: str | None,
    *,
    show_email: bool = False,
) -> list[str]:
    rows: list[list[str]] = []
    for entry in entries:
        row = [
            "*" if entry.alias == active_alias else "",
            entry.alias,
        ]
        if show_email:
            row.append(_format_email(entry.account_email))
        row.extend(
            [
                "" if entry.plan_type is None else entry.plan_type.strip(),
                _format_percent(entry.five_hour_left_percent),
                _format_percent(entry.weekly_left_percent),
            ]
        )
        rows.append(row)

    headers = ["active", "alias"]
    if show_email:
        headers.append("email")
    headers.extend(["type", "5h left", "weekly left"])
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]

    def render(row: list[str]) -> str:
        leading = [cell.ljust(widths[index]) for index, cell in enumerate(row[:-1])]
        return "  ".join([*leading, row[-1]])

    return [
        render(headers),
        render(["-" * width for width in widths]),
        *(render(row) for row in rows),
    ]


def _format_percent(value: int | None) -> str:
    return "?" if value is None else f"{value}%"


def _format_email(value: str | None) -> str:
    if value is None:
        return "?"
    normalized = value.strip()
    return normalized if normalized else "?"


def _resolve_list_format(*, config_format: ListFormat, force_table: bool, force_labelled: bool) -> ListFormat:
    if force_table:
        return ListFormat.TABLE
    if force_labelled:
        return ListFormat.LABELLED
    return config_format


def format_status_lines(status: StatusResult) -> list[str]:
    if status.active_alias is None:
        return [
            "active alias: none",
            f"live auth: {'present' if status.live_auth_exists else 'missing'}",
        ]

    if status.in_sync is True:
        sync_state = "clean"
    elif status.in_sync is False:
        sync_state = "dirty"
    else:
        sync_state = "unknown"

    return [
        f"active alias: {status.active_alias}",
        f"snapshot: {'present' if status.snapshot_exists else 'missing'}",
        f"live auth: {'present' if status.live_auth_exists else 'missing'}",
        f"sync: {sync_state}",
    ]


def format_daemon_status_lines(status: DaemonStatusResult) -> list[str]:
    if status.managed_by == "systemd":
        lines = ["daemon: running" if status.running else "daemon: stopped", "service: codex-switchd.service"]
        if status.service_enabled is not None:
            lines.append(f"enabled: {'yes' if status.service_enabled else 'no'}")
        if status.pid is not None:
            lines.append(f"pid: {status.pid}")
        return lines

    if status.running:
        return [
            "daemon: running",
            f"pid: {status.pid}",
        ]

    if status.stale_pid_file:
        if status.pid is None:
            return [
                "daemon: stopped",
                "pid file: stale",
            ]
        return [
            "daemon: stopped",
            "pid file: stale",
            f"last pid: {status.pid}",
        ]

    return [
        "daemon: stopped",
        "pid file: missing",
    ]


def format_auto_status_lines(status: AutoStatusResult) -> list[str]:
    if status.active_alias is None:
        return [
            "active alias: none",
            "automation: idle",
        ]

    lines = [f"active alias: {status.active_alias}"]
    if status.active_observed_via is None or status.active_observed_at is None:
        lines.append("telemetry: missing")
    else:
        lines.append(f"telemetry: {status.active_observed_via} @ {status.active_observed_at}")
    lines.append(f"soft trigger: {'yes' if status.soft_switch_triggered else 'no'}")
    lines.append(f"target alias: {status.target_alias if status.target_alias is not None else 'none'}")
    return lines


def format_auto_source_lines(rows: list[AutoSourceResult]) -> list[str]:
    if not rows:
        return ["No aliases configured."]

    lines: list[str] = []
    for row in rows:
        if row.observed_via is None or row.observed_at is None:
            lines.append(f"{row.alias}: telemetry missing")
        else:
            lines.append(f"{row.alias}: {row.observed_via} @ {row.observed_at}")
    return lines


def format_auto_history_lines(rows: list[SwitchEventRecord]) -> list[str]:
    if not rows:
        return ["No switch events recorded."]
    return [
        (
            f"{row.id} {row.requested_at} "
            f"{row.from_alias if row.from_alias is not None else '-'}"
            f"->{row.to_alias if row.to_alias is not None else '-'} "
            f"{row.result}"
        )
        for row in rows
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    manager = build_default_manager()

    try:
        if args.command == "add":
            login_mode = LoginMode.DEVICE_AUTH if args.device_auth else LoginMode.BROWSER
            manager.add(args.alias, login_mode=login_mode, isolated=args.isolated)
            print(f"added alias: {args.alias}")
        elif args.command == "use":
            manager.use(args.alias)
            print(f"active alias: {args.alias}")
        elif args.command == "list":
            config = load_app_config(resolve_paths().config_file)
            list_format = _resolve_list_format(
                config_format=config.list_format,
                force_table=args.table,
                force_labelled=args.labelled,
            )
            aliases, active_alias = manager.list_aliases(
                refresh=args.refresh,
                include_email=args.email,
            )
            print(
                *format_alias_lines(
                    aliases,
                    active_alias,
                    list_format,
                    show_email=args.email,
                ),
                sep="\n",
            )
        elif args.command == "remove":
            manager.remove(args.alias)
            print(f"removed alias: {args.alias}")
        elif args.command == "status":
            print(*format_status_lines(manager.status()), sep="\n")
        elif args.command == "daemon":
            if args.daemon_command == "install":
                manager.daemon_install()
                print("daemon installed")
            elif args.daemon_command == "start":
                print(*format_daemon_status_lines(manager.daemon_start()), sep="\n")
            elif args.daemon_command == "stop":
                print(*format_daemon_status_lines(manager.daemon_stop()), sep="\n")
            elif args.daemon_command == "status":
                print(*format_daemon_status_lines(manager.daemon_status()), sep="\n")
            elif args.daemon_command == "enable":
                print(*format_daemon_status_lines(manager.daemon_enable()), sep="\n")
            elif args.daemon_command == "disable":
                print(*format_daemon_status_lines(manager.daemon_disable()), sep="\n")
            else:
                parser.error(f"unknown daemon command: {args.daemon_command}")
        elif args.command == "auto":
            if args.auto_command == "status":
                print(*format_auto_status_lines(manager.auto_status()), sep="\n")
            elif args.auto_command == "source":
                print(*format_auto_source_lines(manager.auto_source()), sep="\n")
            elif args.auto_command == "history":
                print(*format_auto_history_lines(manager.auto_history(limit=args.limit)), sep="\n")
            elif args.auto_command == "retry-resume":
                thread_id = manager.auto_retry_resume()
                print(f"resumed thread: {thread_id}")
            else:
                parser.error(f"unknown auto command: {args.auto_command}")
        else:
            parser.error(f"unknown command: {args.command}")
    except CodexSwitchError as exc:
        parser.exit(1, f"{parser.prog}: error: {exc}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
