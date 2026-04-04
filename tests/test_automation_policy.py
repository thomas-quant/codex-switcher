from __future__ import annotations

from codex_switch.automation_models import RateLimitSnapshot, RateLimitWindow, UsageSource
from codex_switch.automation_policy import choose_target_alias, should_trigger_soft_switch


def make_snapshot(alias: str, primary_used_percent: float | None, secondary_used_percent: float | None) -> RateLimitSnapshot:
    return RateLimitSnapshot(
        alias=alias,
        limit_id=None,
        limit_name="Daily limit",
        observed_via=UsageSource.RPC,
        plan_type="pro",
        primary_window=RateLimitWindow(
            used_percent=primary_used_percent,
            resets_at="2026-04-04T00:00:00Z",
            window_duration_mins=60,
        ),
        secondary_window=RateLimitWindow(
            used_percent=secondary_used_percent,
            resets_at="2026-04-05T00:00:00Z",
            window_duration_mins=10080,
        ),
        credits_has_credits=True,
        credits_unlimited=False,
        credits_balance="5.25",
        observed_at="2026-04-04T00:00:00Z",
    )


def test_should_trigger_soft_switch_at_threshold_boundary() -> None:
    assert should_trigger_soft_switch(make_snapshot("work", 95, 10), 95) is True
    assert should_trigger_soft_switch(make_snapshot("work", 94, 10), 95) is False


def test_choose_target_alias_prefers_lowest_eligible_usage_and_excludes_active_alias() -> None:
    candidates = [
        make_snapshot("work", 1, 2),
        make_snapshot("alpha", 40, 80),
        make_snapshot("beta", 20, 90),
    ]

    assert choose_target_alias("work", candidates, 95) == "beta"
