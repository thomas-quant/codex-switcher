from __future__ import annotations

import subprocess
from typing import cast

import pytest

from codex_switch.codex_login import run_codex_login
from codex_switch.errors import LoginCaptureError
from codex_switch.models import LoginMode


@pytest.mark.parametrize(
    ("mode", "expected_args"),
    [
        (LoginMode.BROWSER, ["codex", "login"]),
        (LoginMode.DEVICE_AUTH, ["codex", "login", "--device-auth"]),
    ],
)
def test_run_codex_login_builds_command_for_mode(monkeypatch, mode, expected_args):
    captured_args = []

    def fake_run(args, **kwargs):
        assert kwargs == {"check": False}
        captured_args.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    run_codex_login(mode)

    assert captured_args == [expected_args]


def test_run_codex_login_defaults_to_browser_mode(monkeypatch):
    captured_args = []

    def fake_run(args, **kwargs):
        assert kwargs == {"check": False}
        captured_args.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    run_codex_login()

    assert captured_args == [["codex", "login"]]


def test_run_codex_login_raises_on_unsupported_mode(monkeypatch):
    def unexpected_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called")

    monkeypatch.setattr(subprocess, "run", unexpected_run)

    with pytest.raises(LoginCaptureError, match="unsupported codex login mode"):
        run_codex_login(cast(LoginMode, "unsupported"))


def test_run_codex_login_normalizes_process_launch_failure(monkeypatch):
    def fail_run(*args, **kwargs):
        raise FileNotFoundError("codex not found")

    monkeypatch.setattr(subprocess, "run", fail_run)

    with pytest.raises(LoginCaptureError, match="codex login did not complete successfully"):
        run_codex_login(LoginMode.BROWSER)


def test_run_codex_login_normalizes_nonzero_return_code(monkeypatch):
    def fake_run(args, **kwargs):
        assert kwargs == {"check": False}
        return subprocess.CompletedProcess(args=args, returncode=1)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(LoginCaptureError, match="codex login did not complete successfully"):
        run_codex_login(LoginMode.BROWSER)
