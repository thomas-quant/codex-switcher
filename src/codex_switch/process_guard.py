from __future__ import annotations

import getpass
import os
from pathlib import Path

import psutil

from codex_switch.errors import CodexProcessRunningError

_CODEX_PROCESS_MESSAGE = (
    "A codex process is running. Exit Codex before mutating account state."
)


def ensure_codex_not_running() -> None:
    current_pid = os.getpid()
    current_user = getpass.getuser()

    for process in psutil.process_iter(["pid", "username", "name", "cmdline"]):
        info = process.info
        pid = info.get("pid")
        if pid == current_pid:
            continue

        if info.get("username") != current_user:
            continue

        if _is_codex_process(info.get("name")) or _is_codex_process_from_cmdline(
            info.get("cmdline")
        ):
            raise CodexProcessRunningError(_CODEX_PROCESS_MESSAGE)


def _is_codex_process(name: object) -> bool:
    if not isinstance(name, str) or not name:
        return False
    return name.lower().split(".")[0] == "codex"


def _is_codex_process_from_cmdline(cmdline: object) -> bool:
    if not isinstance(cmdline, list) or not cmdline:
        return False

    executable = cmdline[0]
    if not isinstance(executable, str) or not executable:
        return False

    return Path(executable).name.lower().split(".")[0] == "codex"
