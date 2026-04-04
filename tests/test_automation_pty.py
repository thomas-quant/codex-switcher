from __future__ import annotations

import pytest

from codex_switch.automation_pty import ParsedStatus
from codex_switch.automation_pty import parse_status_output


def test_parse_status_output_extracts_primary_secondary_usage_and_credits():
    text = """
    Credits: 123.45
    5h limit: 76% used, resets in 2h 30m
    Weekly limit: 91% used, resets in 3d
    """

    assert parse_status_output(text) == ParsedStatus(
        primary_used_percent=76,
        secondary_used_percent=91,
        credits_balance="123.45",
    )


def test_parse_status_output_returns_none_when_known_lines_are_missing():
    assert parse_status_output("No status lines present.") == ParsedStatus(
        primary_used_percent=None,
        secondary_used_percent=None,
        credits_balance=None,
    )


@pytest.mark.parametrize(
    "text, match",
    [
        ("5h limit: seventy six% used, resets in 2h", "5h limit"),
        ("Weekly limit: ninety one% used, resets in 3d", "Weekly limit"),
        ("Credits: one hundred twenty three", "Credits"),
    ],
)
def test_parse_status_output_raises_for_malformed_known_lines(text: str, match: str):
    with pytest.raises(ValueError, match=match):
        parse_status_output(text)
