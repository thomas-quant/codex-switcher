from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import subprocess
from typing import Any

from codex_switch.errors import AutomationSourceUnavailableError

_DEFAULT_APP_SERVER_COMMAND = ("codex", "-s", "read-only", "-a", "untrusted", "app-server")
_RATE_LIMIT_UPDATED_METHOD = "account/rateLimits/updated"
_SOURCE_UNAVAILABLE_MESSAGE = "Codex app-server RPC is unavailable"


def build_rpc_request(request_id: int, method: str, params: Any) -> dict[str, Any]:
    request = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
    }
    if params is not None:
        request["params"] = params
    return request


@dataclass(slots=True, frozen=True)
class ParsedRateLimitNotification:
    primary_used_percent: int
    primary_resets_at: int | None


def parse_rate_limit_notification(notification: Mapping[str, Any]) -> ParsedRateLimitNotification:
    method = notification.get("method")
    if method != _RATE_LIMIT_UPDATED_METHOD:
        raise ValueError(f"Unsupported notification method: {method!r}")

    params = notification.get("params")
    if not isinstance(params, Mapping):
        raise ValueError("rate limit notification params must be a mapping")

    rate_limits = params.get("rateLimits")
    if not isinstance(rate_limits, Mapping):
        raise ValueError("rate limit notification rateLimits must be a mapping")

    if "primary" in rate_limits:
        primary = rate_limits["primary"]
        if not isinstance(primary, Mapping):
            raise ValueError("rate limit notification primary window must be a mapping")
    else:
        primary = rate_limits

    if "usedPercent" not in primary:
        raise ValueError("rate limit notification usedPercent is required")
    used_percent = primary["usedPercent"]
    if not isinstance(used_percent, int) or isinstance(used_percent, bool):
        raise ValueError("rate limit notification usedPercent must be an int")

    resets_at = primary.get("resetsAt")
    if (not isinstance(resets_at, int) or isinstance(resets_at, bool)) and resets_at is not None:
        raise ValueError("rate limit notification resetsAt must be an int or None")

    return ParsedRateLimitNotification(
        primary_used_percent=used_percent,
        primary_resets_at=resets_at,
    )


@dataclass(slots=True)
class CodexRpcClient:
    process: subprocess.Popen[str]

    @classmethod
    def launch_default(cls) -> CodexRpcClient:
        try:
            process = subprocess.Popen(
                list(_DEFAULT_APP_SERVER_COMMAND),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise AutomationSourceUnavailableError(_SOURCE_UNAVAILABLE_MESSAGE) from exc
        return cls(process=process)
