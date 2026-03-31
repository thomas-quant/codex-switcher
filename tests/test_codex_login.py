from __future__ import annotations

import subprocess

import pytest

from codex_switch.codex_login import run_codex_login
from codex_switch.errors import LoginCaptureError


def test_run_codex_login_normalizes_process_launch_failure(monkeypatch):
    def fail_run(*args, **kwargs):
        raise FileNotFoundError("codex not found")

    monkeypatch.setattr(subprocess, "run", fail_run)

    with pytest.raises(LoginCaptureError, match="codex login did not complete successfully"):
        run_codex_login()
