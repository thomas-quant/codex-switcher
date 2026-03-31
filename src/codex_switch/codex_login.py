from __future__ import annotations

import subprocess

from codex_switch.errors import LoginCaptureError


def run_codex_login() -> None:
    try:
        result = subprocess.run(["codex", "login"], check=False)
    except OSError as exc:
        raise LoginCaptureError("codex login did not complete successfully") from exc
    if result.returncode != 0:
        raise LoginCaptureError("codex login did not complete successfully")
