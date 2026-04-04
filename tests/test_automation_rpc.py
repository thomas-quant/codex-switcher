from __future__ import annotations

import subprocess

import pytest

from codex_switch.automation_rpc import (
    CodexRpcClient,
    ParsedRateLimitNotification,
    build_rpc_request,
    parse_rate_limit_notification,
)
from codex_switch.errors import AutomationSourceUnavailableError


def test_build_rpc_request_omits_params_for_none():
    assert build_rpc_request(7, "account/rateLimits/read", None) == {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "account/rateLimits/read",
    }


def test_build_rpc_request_includes_params_for_object_and_list():
    assert build_rpc_request(8, "account/read", {"include": "limits"}) == {
        "jsonrpc": "2.0",
        "id": 8,
        "method": "account/read",
        "params": {"include": "limits"},
    }
    assert build_rpc_request(9, "account/readMany", ["a", "b"]) == {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "account/readMany",
        "params": ["a", "b"],
    }


def test_parse_rate_limit_notification_extracts_primary_usage_fields():
    notification = {
        "jsonrpc": "2.0",
        "method": "account/rateLimits/updated",
        "params": {
            "primary": {
                "usedPercent": 42,
                "resetsAt": "2026-04-04T00:00:00Z",
            },
            "secondary": {
                "usedPercent": 7,
                "resetsAt": "2026-04-04T01:00:00Z",
            },
        },
    }

    assert parse_rate_limit_notification(notification) == ParsedRateLimitNotification(
        primary_used_percent=42,
        primary_resets_at="2026-04-04T00:00:00Z",
    )


def test_parse_rate_limit_notification_raises_for_malformed_primary_envelope():
    notification = {
        "jsonrpc": "2.0",
        "method": "account/rateLimits/updated",
        "params": {
            "primary": 42,
            "usedPercent": 42,
            "resetsAt": "2026-04-04T00:00:00Z",
        },
    }

    with pytest.raises(ValueError, match="primary window"):
        parse_rate_limit_notification(notification)


def test_codex_rpc_client_launch_default_uses_app_server_command(monkeypatch):
    captured = {}

    class DummyProcess:
        pass

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return DummyProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    client = CodexRpcClient.launch_default()

    assert isinstance(client, CodexRpcClient)
    assert isinstance(client.process, DummyProcess)
    assert captured["args"] == ["codex", "-s", "read-only", "-a", "untrusted", "app-server"]
    assert captured["kwargs"]["stdin"] == subprocess.PIPE
    assert captured["kwargs"]["stdout"] == subprocess.PIPE
    assert captured["kwargs"]["stderr"] == subprocess.PIPE
    assert captured["kwargs"]["text"] is True


def test_codex_rpc_client_launch_default_normalizes_spawn_failure(monkeypatch):
    def fail_popen(*args, **kwargs):
        raise FileNotFoundError("codex not found")

    monkeypatch.setattr(subprocess, "Popen", fail_popen)

    with pytest.raises(AutomationSourceUnavailableError, match="Codex app-server"):
        CodexRpcClient.launch_default()
