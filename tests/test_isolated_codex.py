from __future__ import annotations

from pathlib import Path

from codex_switch.isolated_codex import isolated_codex_env


def test_isolated_codex_env_creates_codex_home_without_auth_bytes():
    with isolated_codex_env() as env:
        home = Path(env["HOME"])
        codex_home = Path(env["CODEX_HOME"])

        assert codex_home == home / ".codex"
        assert codex_home.exists()
        assert codex_home.is_dir()
        assert not (codex_home / "auth.json").exists()


def test_isolated_codex_env_uses_switch_root_instead_of_system_tmp(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

    with isolated_codex_env() as env:
        home = Path(env["HOME"])

        assert home.parent == fake_home / ".codex-switch" / "isolated-homes"
