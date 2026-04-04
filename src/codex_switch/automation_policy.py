from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple

from codex_switch.automation_models import RateLimitSnapshot


class _TargetScore(NamedTuple):
    tier: int
    used_percent: float
    alias: str


def should_trigger_soft_switch(snapshot: RateLimitSnapshot, threshold: float) -> bool:
    return any(
        used_percent is not None and used_percent >= threshold
        for used_percent in (
            snapshot.primary_window.used_percent,
            snapshot.secondary_window.used_percent,
        )
    )


def choose_target_alias(
    active_alias: str,
    candidates: Iterable[RateLimitSnapshot],
    threshold: float,
) -> str | None:
    best_score: _TargetScore | None = None

    for snapshot in candidates:
        if snapshot.alias == active_alias:
            continue

        score = _score_snapshot(snapshot, threshold)
        if score is None:
            continue

        if best_score is None or score < best_score:
            best_score = score

    if best_score is None:
        return None
    return best_score.alias


def _score_snapshot(snapshot: RateLimitSnapshot, threshold: float) -> _TargetScore | None:
    primary_used_percent = snapshot.primary_window.used_percent
    if primary_used_percent is not None and primary_used_percent < threshold:
        return _TargetScore(0, primary_used_percent, snapshot.alias)

    secondary_used_percent = snapshot.secondary_window.used_percent
    if secondary_used_percent is not None and secondary_used_percent < threshold:
        return _TargetScore(1, secondary_used_percent, snapshot.alias)

    return None
