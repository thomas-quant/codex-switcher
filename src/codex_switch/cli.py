from __future__ import annotations

import argparse
from collections.abc import Sequence

from codex_switch.accounts import AccountStore
from codex_switch.errors import CodexSwitchError
from codex_switch.manager import CodexSwitchManager
from codex_switch.models import StatusResult
from codex_switch.paths import resolve_paths
from codex_switch.state import StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-switch")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("add", "use", "list", "remove", "status"):
        child = subparsers.add_parser(name)
        if name in {"add", "use", "remove"}:
            child.add_argument("alias")

    return parser


def build_default_manager() -> CodexSwitchManager:
    from codex_switch.codex_login import run_codex_login
    from codex_switch.process_guard import ensure_codex_not_running

    paths = resolve_paths()
    accounts = AccountStore(paths.accounts_dir)
    state = StateStore(paths.state_file)
    return CodexSwitchManager(
        paths=paths,
        accounts=accounts,
        state=state,
        ensure_safe_to_mutate=ensure_codex_not_running,
        login_runner=run_codex_login,
    )


def format_alias_lines(aliases: list[str], active_alias: str | None) -> list[str]:
    if not aliases:
        return ["No aliases configured."]
    return [f"* {alias}" if alias == active_alias else f"  {alias}" for alias in aliases]


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    manager = build_default_manager()

    try:
        if args.command == "add":
            manager.add(args.alias)
            print(f"added alias: {args.alias}")
        elif args.command == "use":
            manager.use(args.alias)
            print(f"active alias: {args.alias}")
        elif args.command == "list":
            aliases, active_alias = manager.list_aliases()
            print(*format_alias_lines(aliases, active_alias), sep="\n")
        elif args.command == "remove":
            manager.remove(args.alias)
            print(f"removed alias: {args.alias}")
        elif args.command == "status":
            print(*format_status_lines(manager.status()), sep="\n")
        else:
            parser.error(f"unknown command: {args.command}")
    except CodexSwitchError as exc:
        parser.exit(1, f"{parser.prog}: error: {exc}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
