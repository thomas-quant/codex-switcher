from __future__ import annotations

import subprocess

from codex_switch.errors import LoginCaptureError
from codex_switch.models import LoginMode


def run_codex_login(mode: LoginMode = LoginMode.BROWSER) -> None:
    if mode is LoginMode.BROWSER:
        command = ["codex", "login"]
    elif mode is LoginMode.DEVICE_AUTH:
        command = ["codex", "login", "--device-auth"]
    else:
        raise LoginCaptureError("unsupported codex login mode")
    try:
        result = subprocess.run(command, check=False)
    except OSError as exc:
        raise LoginCaptureError("codex login did not complete successfully") from exc
    if result.returncode != 0:
        raise LoginCaptureError("codex login did not complete successfully")
