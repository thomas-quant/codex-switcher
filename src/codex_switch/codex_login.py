from __future__ import annotations

import subprocess

from codex_switch.errors import LoginCaptureError


def run_codex_login() -> None:
    result = subprocess.run(["codex", "login"], check=False)
    if result.returncode != 0:
        raise LoginCaptureError("codex login did not complete successfully")
