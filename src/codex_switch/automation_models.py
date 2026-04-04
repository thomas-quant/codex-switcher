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
