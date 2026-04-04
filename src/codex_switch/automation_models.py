from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class UsageSource(str, Enum):
    RPC = "RPC"
    PTY = "PTY"


class HandoffPhase(str, Enum):
    pending_idle_checkpoint = "pending_idle_checkpoint"
    pending_stop = "pending_stop"
    pending_switch = "pending_switch"
    pending_resume = "pending_resume"
    failed_resume = "failed_resume"


@dataclass(slots=True, frozen=True)
class RateLimitWindow:
    used_percent: float | None
    resets_at: str | None
    window_duration_mins: int | None


@dataclass(slots=True, frozen=True)
class RateLimitSnapshot:
    alias: str
    limit_id: str | None
    limit_name: str
    observed_via: UsageSource
    plan_type: str | None
    primary_window: RateLimitWindow
    secondary_window: RateLimitWindow
    credits_has_credits: bool | None
    credits_unlimited: bool | None
    credits_balance: str | None
    observed_at: str


@dataclass(slots=True, frozen=True)
class AccountIdentitySnapshot:
    email: str | None
    plan_type: str | None
    fingerprint: str | None
    observed_at: str


@dataclass(slots=True, frozen=True)
class ThreadRuntimeSnapshot:
    thread_id: str
    cwd: str | None
    model: str | None
    current_alias: str | None
    last_turn_id: str | None
    last_known_status: str | None
    safe_to_switch: bool
    last_total_tokens: int | None
    last_seen_at: str


@dataclass(slots=True, frozen=True)
class ThreadTurnUsageSnapshot:
    thread_id: str
    turn_id: str
    last_input_tokens: int | None
    last_cached_input_tokens: int | None
    last_output_tokens: int | None
    last_reasoning_output_tokens: int | None
    last_total_tokens: int | None
    total_input_tokens: int | None
    total_cached_input_tokens: int | None
    total_output_tokens: int | None
    total_reasoning_output_tokens: int | None
    total_tokens: int | None
    observed_at: str
