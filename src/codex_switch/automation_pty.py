from __future__ import annotations

from dataclasses import dataclass
import re

_CREDITS_RE = re.compile(r"^\s*Credits:\s*(?P<credits>\d+(?:\.\d+)?)\s*$", re.IGNORECASE | re.MULTILINE)
_PRIMARY_LIMIT_RE = re.compile(
    r"^\s*5h limit:\s*(?P<used_percent>\d+)% used\b",
    re.IGNORECASE | re.MULTILINE,
)
_SECONDARY_LIMIT_RE = re.compile(
    r"^\s*Weekly limit:\s*(?P<used_percent>\d+)% used\b",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(slots=True, frozen=True)
class ParsedStatus:
    primary_used_percent: int | None
    secondary_used_percent: int | None
    credits_balance: float | None


def _parse_int(pattern: re.Pattern[str], text: str) -> int | None:
    match = pattern.search(text)
    if match is None:
        return None
    return int(match.group("used_percent"))


def _parse_float(pattern: re.Pattern[str], text: str) -> float | None:
    match = pattern.search(text)
    if match is None:
        return None
    return float(match.group("credits"))


def parse_status_output(text: str) -> ParsedStatus:
    return ParsedStatus(
        primary_used_percent=_parse_int(_PRIMARY_LIMIT_RE, text),
        secondary_used_percent=_parse_int(_SECONDARY_LIMIT_RE, text),
        credits_balance=_parse_float(_CREDITS_RE, text),
    )
