from __future__ import annotations

import subprocess

import pytest

from codex_switch.automation_models import (
    AccountIdentitySnapshot,
    RateLimitSnapshot,
    RateLimitWindow,
    ThreadRuntimeSnapshot,
    ThreadTurnUsageSnapshot,
    UsageSource,
)
from codex_switch.automation_rpc import (
    CodexRpcClient,
    RpcMessage,
    ParsedRateLimitNotification,
    build_rpc_request,
    parse_account_read_result,
    parse_rate_limits_result,
    parse_rate_limit_notification,
    parse_thread_runtime_notification,
    parse_thread_turn_usage_notification,
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
            "rateLimits": {
                "primary": {
                    "usedPercent": 42,
                    "resetsAt": 1_744_147_200,
                },
                "secondary": {
                    "usedPercent": 7,
                    "resetsAt": 1_744_150_800,
                },
            },
        },
    }

    assert parse_rate_limit_notification(notification) == ParsedRateLimitNotification(
        primary_used_percent=42,
        primary_resets_at=1_744_147_200,
    )


def test_parse_rate_limit_notification_raises_for_malformed_primary_envelope():
    notification = {
        "jsonrpc": "2.0",
        "method": "account/rateLimits/updated",
        "params": {
            "rateLimits": {
                "primary": 42,
                "usedPercent": 42,
                "resetsAt": 1_744_147_200,
            },
        },
    }

    with pytest.raises(ValueError, match="primary window"):
        parse_rate_limit_notification(notification)


def test_parse_rate_limit_notification_uses_flat_fallback_when_primary_missing():
    notification = {
        "jsonrpc": "2.0",
        "method": "account/rateLimits/updated",
        "params": {
            "rateLimits": {
                "usedPercent": 88,
                "resetsAt": None,
            }
        },
    }

    assert parse_rate_limit_notification(notification) == ParsedRateLimitNotification(
        primary_used_percent=88,
        primary_resets_at=None,
    )


def test_parse_rate_limit_notification_raises_for_missing_used_percent():
    notification = {
        "jsonrpc": "2.0",
        "method": "account/rateLimits/updated",
        "params": {
            "rateLimits": {
                "primary": {
                    "resetsAt": None,
                }
            }
        },
    }

    with pytest.raises(ValueError, match="usedPercent"):
        parse_rate_limit_notification(notification)


def test_parse_rate_limit_notification_raises_for_non_int_used_percent():
    notification = {
        "jsonrpc": "2.0",
        "method": "account/rateLimits/updated",
        "params": {
            "rateLimits": {
                "primary": {
                    "usedPercent": "42",
                    "resetsAt": None,
                }
            }
        },
    }

    with pytest.raises(ValueError, match="usedPercent"):
        parse_rate_limit_notification(notification)


def test_parse_rate_limit_notification_raises_for_invalid_resets_at_type():
    notification = {
        "jsonrpc": "2.0",
        "method": "account/rateLimits/updated",
        "params": {
            "rateLimits": {
                "primary": {
                    "usedPercent": 42,
                    "resetsAt": "2026-04-04T00:00:00Z",
                }
            }
        },
    }

    with pytest.raises(ValueError, match="resetsAt"):
        parse_rate_limit_notification(notification)


def test_parse_account_read_result_extracts_identity_snapshot():
    response = {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {
            "email": "work@example.com",
            "planType": "pro",
            "fingerprint": "fp-work",
        },
    }

    assert parse_account_read_result(response, observed_at="2026-04-05T00:00:00Z") == AccountIdentitySnapshot(
        email="work@example.com",
        plan_type="pro",
        fingerprint="fp-work",
        observed_at="2026-04-05T00:00:00Z",
    )


def test_parse_rate_limits_result_extracts_full_snapshot_list():
    response = {
        "jsonrpc": "2.0",
        "id": 4,
        "result": {
            "planType": "pro",
            "credits": {
                "hasCredits": True,
                "unlimited": False,
                "balance": "5.25",
            },
            "rateLimits": [
                {
                    "id": "five-hour",
                    "name": "5h limit",
                    "primary": {
                        "usedPercent": 42,
                        "resetsAt": 1_744_147_200,
                        "windowDurationMins": 300,
                    },
                    "secondary": {
                        "usedPercent": 70,
                        "resetsAt": 1_744_752_000,
                        "windowDurationMins": 10080,
                    },
                }
            ],
        },
    }

    assert parse_rate_limits_result(
        alias="work",
        response=response,
        observed_via=UsageSource.RPC,
        observed_at="2026-04-05T00:00:00Z",
    ) == [
        RateLimitSnapshot(
            alias="work",
            limit_id="five-hour",
            limit_name="5h limit",
            observed_via=UsageSource.RPC,
            plan_type="pro",
            primary_window=RateLimitWindow(
                used_percent=42,
                resets_at="2025-04-08T21:20:00Z",
                window_duration_mins=300,
            ),
            secondary_window=RateLimitWindow(
                used_percent=70,
                resets_at="2025-04-15T21:20:00Z",
                window_duration_mins=10080,
            ),
            credits_has_credits=True,
            credits_unlimited=False,
            credits_balance="5.25",
            observed_at="2026-04-05T00:00:00Z",
        )
    ]


def test_parse_rate_limits_result_accepts_singleton_mapping_shape():
    response = {
        "jsonrpc": "2.0",
        "id": 4,
        "result": {
            "rateLimits": {
                "limitId": "codex",
                "limitName": None,
                "primary": {
                    "usedPercent": 97,
                    "resetsAt": 1_775_372_244,
                    "windowDurationMins": 300,
                },
                "secondary": {
                    "usedPercent": 54,
                    "resetsAt": 1_775_834_532,
                    "windowDurationMins": 10080,
                },
                "credits": {
                    "hasCredits": False,
                    "unlimited": False,
                    "balance": None,
                },
                "planType": "team",
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "limitName": None,
                    "primary": {
                        "usedPercent": 97,
                        "resetsAt": 1_775_372_244,
                        "windowDurationMins": 300,
                    },
                    "secondary": {
                        "usedPercent": 54,
                        "resetsAt": 1_775_834_532,
                        "windowDurationMins": 10080,
                    },
                    "credits": {
                        "hasCredits": False,
                        "unlimited": False,
                        "balance": None,
                    },
                    "planType": "team",
                }
            },
        },
    }

    assert parse_rate_limits_result(
        alias="work",
        response=response,
        observed_via=UsageSource.RPC,
        observed_at="2026-04-05T00:00:00Z",
    ) == [
        RateLimitSnapshot(
            alias="work",
            limit_id="codex",
            limit_name="codex",
            observed_via=UsageSource.RPC,
            plan_type="team",
            primary_window=RateLimitWindow(
                used_percent=97,
                resets_at="2026-04-05T06:57:24Z",
                window_duration_mins=300,
            ),
            secondary_window=RateLimitWindow(
                used_percent=54,
                resets_at="2026-04-10T15:22:12Z",
                window_duration_mins=10080,
            ),
            credits_has_credits=False,
            credits_unlimited=False,
            credits_balance=None,
            observed_at="2026-04-05T00:00:00Z",
        )
    ]


def test_parse_thread_runtime_notification_extracts_safe_checkpoint_state():
    notification = {
        "jsonrpc": "2.0",
        "method": "thread/runtime/updated",
        "params": {
            "threadId": "thread-1",
            "cwd": "/repo",
            "model": "gpt-5.4",
            "turnId": "turn-2",
            "status": "idle",
            "safeToSwitch": True,
            "lastTotalTokens": 321,
        },
    }

    assert parse_thread_runtime_notification(
        notification,
        current_alias="work",
        observed_at="2026-04-05T00:00:00Z",
    ) == ThreadRuntimeSnapshot(
        thread_id="thread-1",
        cwd="/repo",
        model="gpt-5.4",
        current_alias="work",
        last_turn_id="turn-2",
        last_known_status="idle",
        safe_to_switch=True,
        last_total_tokens=321,
        last_seen_at="2026-04-05T00:00:00Z",
    )


def test_parse_thread_turn_usage_notification_extracts_delta_and_total_counters():
    notification = {
        "jsonrpc": "2.0",
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-2",
            "lastUsage": {
                "inputTokens": 3,
                "cachedInputTokens": 1,
                "outputTokens": 5,
                "reasoningOutputTokens": 2,
                "totalTokens": 11,
            },
            "totalUsage": {
                "inputTokens": 13,
                "cachedInputTokens": 2,
                "outputTokens": 15,
                "reasoningOutputTokens": 3,
                "totalTokens": 33,
            },
        },
    }

    assert parse_thread_turn_usage_notification(
        notification,
        observed_at="2026-04-05T00:00:00Z",
    ) == ThreadTurnUsageSnapshot(
        thread_id="thread-1",
        turn_id="turn-2",
        last_input_tokens=3,
        last_cached_input_tokens=1,
        last_output_tokens=5,
        last_reasoning_output_tokens=2,
        last_total_tokens=11,
        total_input_tokens=13,
        total_cached_input_tokens=2,
        total_output_tokens=15,
        total_reasoning_output_tokens=3,
        total_tokens=33,
        observed_at="2026-04-05T00:00:00Z",
    )


def test_codex_rpc_client_send_request_writes_json_and_reads_response(monkeypatch):
    class DummyStream:
        def __init__(self, lines: list[str]) -> None:
            self._lines = list(lines)
            self.writes: list[str] = []
            self.flush_calls = 0

        def write(self, chunk: str) -> int:
            self.writes.append(chunk)
            return len(chunk)

        def flush(self) -> None:
            self.flush_calls += 1

        def readline(self) -> str:
            if not self._lines:
                return ""
            return self._lines.pop(0)

    class DummyProcess:
        def __init__(self) -> None:
            self.stdin = DummyStream([])
            self.stdout = DummyStream(['{"jsonrpc":"2.0","id":7,"result":{"ok":true}}\n'])
            self.stderr = DummyStream([])

    client = CodexRpcClient(process=DummyProcess())

    response = client.send_request(7, "account/read", {"include": "limits"})

    assert response == RpcMessage(payload={"jsonrpc": "2.0", "id": 7, "result": {"ok": True}})
    assert client.process.stdin.writes == [
        '{"jsonrpc":"2.0","id":7,"method":"account/read","params":{"include":"limits"}}\n'
    ]
    assert client.process.stdin.flush_calls == 1


def test_codex_rpc_client_send_request_buffers_interleaved_notification_until_response():
    class DummyStream:
        def __init__(self, lines: list[str]) -> None:
            self._lines = list(lines)
            self.writes: list[str] = []

        def write(self, chunk: str) -> int:
            self.writes.append(chunk)
            return len(chunk)

        def flush(self) -> None:
            return None

        def readline(self) -> str:
            if not self._lines:
                return ""
            return self._lines.pop(0)

    class DummyProcess:
        def __init__(self) -> None:
            self.stdin = DummyStream([])
            self.stdout = DummyStream(
                [
                    '{"jsonrpc":"2.0","method":"thread/runtime/updated","params":{"threadId":"thread-1","safeToSwitch":true}}\n',
                    '{"jsonrpc":"2.0","id":8,"result":{"ok":true}}\n',
                ]
            )
            self.stderr = DummyStream([])

    client = CodexRpcClient(process=DummyProcess())

    response = client.send_request(8, "account/read", None)

    assert response.payload["id"] == 8
    assert client.read_message_nonblocking().payload["method"] == "thread/runtime/updated"


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
