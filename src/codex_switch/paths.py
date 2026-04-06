from __future__ import annotations

from pathlib import Path

from codex_switch.models import AppPaths


def resolve_paths(home: Path | None = None) -> AppPaths:
    root = Path(home) if home is not None else Path.home()
    codex_root = root / ".codex"
    switch_root = root / ".codex-switch"
    return AppPaths(
        home=root,
        codex_root=codex_root,
        live_auth_file=codex_root / "auth.json",
        switch_root=switch_root,
        automation_db_file=switch_root / "automation.sqlite",
        daemon_pid_file=switch_root / "daemon.pid",
        daemon_log_dir=switch_root / "logs",
        accounts_dir=switch_root / "accounts",
        state_file=switch_root / "state.json",
        config_file=switch_root / "config.json",
    )
