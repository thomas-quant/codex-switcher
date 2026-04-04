from __future__ import annotations

from codex_switch.automation_pty import ParsedStatus
from codex_switch.automation_pty import parse_status_output


def test_parse_status_output_extracts_primary_secondary_usage_and_credits():
    text = """
    Credits: 123.45
    5h limit: 76% used, resets in 2h
    Weekly limit: 91% used, resets in 3d
    """

    assert parse_status_output(text) == ParsedStatus(
        primary_used_percent=76,
        secondary_used_percent=91,
        credits_balance=123.45,
    )
