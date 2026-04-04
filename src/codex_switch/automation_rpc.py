from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import select
import subprocess
from typing import Any

from codex_switch.automation_models import (
    AccountIdentitySnapshot,
    RateLimitSnapshot,
    RateLimitWindow,
    ThreadRuntimeSnapshot,
    ThreadTurnUsageSnapshot,
    UsageSource,
)
from codex_switch.errors import AutomationSourceUnavailableError

_DEFAULT_APP_SERVER_COMMAND = ("codex", "-s", "read-only", "-a", "untrusted", "app-server")
_RATE_LIMIT_UPDATED_METHOD = "account/rateLimits/updated"
_THREAD_RUNTIME_UPDATED_METHOD = "thread/runtime/updated"
_THREAD_TOKEN_USAGE_UPDATED_METHOD = "thread/tokenUsage/updated"
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


@dataclass(slots=True, frozen=True)
class RpcMessage:
    payload: dict[str, Any]


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


def parse_account_read_result(
    response: Mapping[str, Any],
    *,
    observed_at: str,
) -> AccountIdentitySnapshot:
    result = _mapping_field(response, "result", context="account/read response")
    account = result.get("account")
    if account is None:
        account = result
    if not isinstance(account, Mapping):
        raise ValueError("account/read response account must be a mapping")

    return AccountIdentitySnapshot(
        email=_optional_str(account.get("email"), context="account/read email"),
        plan_type=_optional_str(account.get("planType"), context="account/read planType"),
        fingerprint=_optional_str(account.get("fingerprint"), context="account/read fingerprint"),
        observed_at=observed_at,
    )


def parse_rate_limits_result(
    *,
    alias: str,
    response: Mapping[str, Any],
    observed_via: UsageSource,
    observed_at: str,
) -> list[RateLimitSnapshot]:
    result = _mapping_field(response, "result", context="account/rateLimits/read response")
    plan_type = _optional_str(result.get("planType"), context="rateLimits planType")
    credits = result.get("credits")
    if credits is None:
        credits_mapping: Mapping[str, Any] | None = None
    elif isinstance(credits, Mapping):
        credits_mapping = credits
    else:
        raise ValueError("account/rateLimits/read credits must be a mapping or null")

    credits_has_credits = _optional_bool(
        None if credits_mapping is None else credits_mapping.get("hasCredits"),
        context="rateLimits credits.hasCredits",
    )
    credits_unlimited = _optional_bool(
        None if credits_mapping is None else credits_mapping.get("unlimited"),
        context="rateLimits credits.unlimited",
    )
    credits_balance = _optional_decimal_string(
        None if credits_mapping is None else credits_mapping.get("balance"),
        context="rateLimits credits.balance",
    )

    raw_rate_limits = result.get("rateLimits")
    if isinstance(raw_rate_limits, list):
        items = raw_rate_limits
    elif isinstance(raw_rate_limits, Mapping) and isinstance(raw_rate_limits.get("items"), list):
        items = raw_rate_limits["items"]
    else:
        raise ValueError("account/rateLimits/read rateLimits must be a list")

    snapshots: list[RateLimitSnapshot] = []
    for raw_item in items:
        if not isinstance(raw_item, Mapping):
            raise ValueError("account/rateLimits/read rate limit item must be a mapping")
        snapshots.append(
            RateLimitSnapshot(
                alias=alias,
                limit_id=_optional_str(raw_item.get("id"), context="rateLimits id"),
                limit_name=_required_str(raw_item.get("name"), context="rateLimits name"),
                observed_via=observed_via,
                plan_type=plan_type,
                primary_window=_parse_rate_limit_window(raw_item.get("primary"), context="rateLimits primary"),
                secondary_window=_parse_rate_limit_window(
                    raw_item.get("secondary"),
                    context="rateLimits secondary",
                ),
                credits_has_credits=credits_has_credits,
                credits_unlimited=credits_unlimited,
                credits_balance=credits_balance,
                observed_at=observed_at,
            )
        )
    return snapshots


def parse_thread_runtime_notification(
    notification: Mapping[str, Any],
    *,
    current_alias: str | None,
    observed_at: str,
) -> ThreadRuntimeSnapshot:
    method = notification.get("method")
    if method != _THREAD_RUNTIME_UPDATED_METHOD:
        raise ValueError(f"Unsupported notification method: {method!r}")

    params = _mapping_field(notification, "params", context="thread/runtime notification")
    return ThreadRuntimeSnapshot(
        thread_id=_required_str(params.get("threadId"), context="thread runtime threadId"),
        cwd=_optional_str(params.get("cwd"), context="thread runtime cwd"),
        model=_optional_str(params.get("model"), context="thread runtime model"),
        current_alias=current_alias,
        last_turn_id=_optional_str(params.get("turnId"), context="thread runtime turnId"),
        last_known_status=_optional_str(params.get("status"), context="thread runtime status"),
        safe_to_switch=_required_bool(params.get("safeToSwitch"), context="thread runtime safeToSwitch"),
        last_total_tokens=_optional_int(
            params.get("lastTotalTokens"),
            context="thread runtime lastTotalTokens",
        ),
        last_seen_at=observed_at,
    )


def parse_thread_turn_usage_notification(
    notification: Mapping[str, Any],
    *,
    observed_at: str,
) -> ThreadTurnUsageSnapshot:
    method = notification.get("method")
    if method != _THREAD_TOKEN_USAGE_UPDATED_METHOD:
        raise ValueError(f"Unsupported notification method: {method!r}")

    params = _mapping_field(notification, "params", context="thread/tokenUsage notification")
    last_usage = _mapping_field(params, "lastUsage", context="thread/tokenUsage lastUsage")
    total_usage = _mapping_field(params, "totalUsage", context="thread/tokenUsage totalUsage")

    return ThreadTurnUsageSnapshot(
        thread_id=_required_str(params.get("threadId"), context="thread tokenUsage threadId"),
        turn_id=_required_str(params.get("turnId"), context="thread tokenUsage turnId"),
        last_input_tokens=_optional_int(last_usage.get("inputTokens"), context="lastUsage inputTokens"),
        last_cached_input_tokens=_optional_int(
            last_usage.get("cachedInputTokens"),
            context="lastUsage cachedInputTokens",
        ),
        last_output_tokens=_optional_int(last_usage.get("outputTokens"), context="lastUsage outputTokens"),
        last_reasoning_output_tokens=_optional_int(
            last_usage.get("reasoningOutputTokens"),
            context="lastUsage reasoningOutputTokens",
        ),
        last_total_tokens=_optional_int(last_usage.get("totalTokens"), context="lastUsage totalTokens"),
        total_input_tokens=_optional_int(total_usage.get("inputTokens"), context="totalUsage inputTokens"),
        total_cached_input_tokens=_optional_int(
            total_usage.get("cachedInputTokens"),
            context="totalUsage cachedInputTokens",
        ),
        total_output_tokens=_optional_int(total_usage.get("outputTokens"), context="totalUsage outputTokens"),
        total_reasoning_output_tokens=_optional_int(
            total_usage.get("reasoningOutputTokens"),
            context="totalUsage reasoningOutputTokens",
        ),
        total_tokens=_optional_int(total_usage.get("totalTokens"), context="totalUsage totalTokens"),
        observed_at=observed_at,
    )


@dataclass(slots=True)
class CodexRpcClient:
    process: subprocess.Popen[str]
    _buffered_messages: list[RpcMessage] = field(default_factory=list)

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

    def send_request(self, request_id: int, method: str, params: Any) -> RpcMessage:
        if self.process.stdin is None or self.process.stdout is None:
            raise AutomationSourceUnavailableError(_SOURCE_UNAVAILABLE_MESSAGE)
        payload = build_rpc_request(request_id, method, params)
        self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.process.stdin.flush()
        while True:
            message = self._read_stream_message()
            if message is None:
                raise AutomationSourceUnavailableError("Codex app-server RPC closed unexpectedly")
            if message.payload.get("id") == request_id:
                return message
            self._buffered_messages.append(message)

    def read_message(self) -> RpcMessage | None:
        if self._buffered_messages:
            return self._buffered_messages.pop(0)
        return self._read_stream_message()

    def read_message_nonblocking(self) -> RpcMessage | None:
        if self._buffered_messages:
            return self._buffered_messages.pop(0)
        if self.process.stdout is None:
            raise AutomationSourceUnavailableError(_SOURCE_UNAVAILABLE_MESSAGE)
        if not hasattr(self.process.stdout, "fileno"):
            return None
        try:
            ready, _write_ready, _errors = select.select([self.process.stdout], [], [], 0)
        except (OSError, ValueError):
            return None
        if not ready:
            return None
        return self._read_stream_message()

    def drain_messages_nonblocking(self) -> list[RpcMessage]:
        messages: list[RpcMessage] = []
        while True:
            message = self.read_message_nonblocking()
            if message is None:
                return messages
            messages.append(message)

    def _read_stream_message(self) -> RpcMessage | None:
        if self.process.stdout is None:
            raise AutomationSourceUnavailableError(_SOURCE_UNAVAILABLE_MESSAGE)
        raw_line = self.process.stdout.readline()
        if raw_line == "":
            return None
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed JSON-RPC message: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON-RPC message payload must be an object")
        return RpcMessage(payload=payload)


def _parse_rate_limit_window(value: Any, *, context: str) -> RateLimitWindow:
    if value is None:
        return RateLimitWindow(
            used_percent=None,
            resets_at=None,
            window_duration_mins=None,
        )
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} must be a mapping or null")
    return RateLimitWindow(
        used_percent=_optional_float(value.get("usedPercent"), context=f"{context} usedPercent"),
        resets_at=_optional_epoch_seconds_to_iso(
            value.get("resetsAt"),
            context=f"{context} resetsAt",
        ),
        window_duration_mins=_optional_int(
            value.get("windowDurationMins"),
            context=f"{context} windowDurationMins",
        ),
    )


def _mapping_field(value: Mapping[str, Any], key: str, *, context: str) -> Mapping[str, Any]:
    field = value.get(key)
    if not isinstance(field, Mapping):
        raise ValueError(f"{context} {key} must be a mapping")
    return field


def _required_str(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{context} must be a non-empty string")
    return value


def _optional_str(value: Any, *, context: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{context} must be a string or null")
    return value


def _required_bool(value: Any, *, context: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{context} must be a bool")
    return value


def _optional_bool(value: Any, *, context: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{context} must be a bool or null")
    return value


def _optional_int(value: Any, *, context: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{context} must be an int or null")
    return value


def _optional_float(value: Any, *, context: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{context} must be a number or null")
    return float(value)


def _optional_decimal_string(value: Any, *, context: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{context} must be a string, number, or null")
    return str(value)


def _optional_epoch_seconds_to_iso(value: Any, *, context: str) -> str | None:
    parsed = _optional_int(value, context=context)
    if parsed is None:
        return None
    return datetime.fromtimestamp(parsed, tz=timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )
