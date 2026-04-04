from __future__ import annotations

from dataclasses import dataclass
import re

_CREDITS_RE = re.compile(r"^\s*Credits:\s*(?P<credits>\d+(?:\.\d+)?)\s*$", re.IGNORECASE)
_PRIMARY_LIMIT_RE = re.compile(
    r"^\s*5h limit:\s*(?P<used_percent>\d+)% used,\s*resets in\s+(?P<resets_after>.+?)\s*$",
    re.IGNORECASE,
)
_SECONDARY_LIMIT_RE = re.compile(
    r"^\s*Weekly limit:\s*(?P<used_percent>\d+)% used,\s*resets in\s+(?P<resets_after>.+?)\s*$",
    re.IGNORECASE,
)


@dataclass(slots=True, frozen=True)
class ParsedStatus:
    primary_used_percent: int | None
    secondary_used_percent: int | None
    credits_balance: str | None


def _parse_label_line(
    pattern: re.Pattern[str],
    label: str,
    text: str,
    group_name: str,
    converter,
):
    found_value = None
    label_prefix = label.casefold()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.casefold().startswith(label_prefix):
            match = pattern.fullmatch(line)
            if match is None:
                raise ValueError(f"Malformed {label} line: {line}")
            found_value = converter(match.group(group_name))
    return found_value


def parse_status_output(text: str) -> ParsedStatus:
    return ParsedStatus(
        primary_used_percent=_parse_label_line(
            _PRIMARY_LIMIT_RE,
            "5h limit:",
            text,
            "used_percent",
            int,
        ),
        secondary_used_percent=_parse_label_line(
            _SECONDARY_LIMIT_RE,
            "Weekly limit:",
            text,
            "used_percent",
            int,
        ),
        credits_balance=_parse_label_line(
            _CREDITS_RE,
            "Credits:",
            text,
            "credits",
            str,
        ),
    )
