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
    return {
        "id": request_id,
        "method": method,
        "params": params,
    }


@dataclass(slots=True, frozen=True)
class ParsedRateLimitNotification:
    primary_used_percent: float | None
    primary_resets_at: str | None


def parse_rate_limit_notification(notification: Mapping[str, Any]) -> ParsedRateLimitNotification:
    method = notification.get("method")
    if method != _RATE_LIMIT_UPDATED_METHOD:
        raise ValueError(f"Unsupported notification method: {method!r}")

    params = notification.get("params")
    if not isinstance(params, Mapping):
        raise ValueError("rate limit notification params must be a mapping")

    primary = params.get("primary")
    if not isinstance(primary, Mapping):
        primary = params
    if not isinstance(primary, Mapping):
        raise ValueError("rate limit notification primary window must be a mapping")

    return ParsedRateLimitNotification(
        primary_used_percent=primary.get("usedPercent"),
        primary_resets_at=primary.get("resetsAt"),
    )


@dataclass(slots=True)
class CodexRpcClient:
    command: tuple[str, ...] = _DEFAULT_APP_SERVER_COMMAND

    def launch_default(self) -> subprocess.Popen[str]:
        try:
            return subprocess.Popen(
                list(self.command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise AutomationSourceUnavailableError(_SOURCE_UNAVAILABLE_MESSAGE) from exc
