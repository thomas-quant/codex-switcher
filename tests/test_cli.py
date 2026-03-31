import pytest

from codex_switch.cli import build_parser
from codex_switch.cli import main


def test_build_parser_registers_expected_subcommands():
    parser = build_parser()
    subparsers = next(action for action in parser._actions if getattr(action, "choices", None))
    assert set(subparsers.choices) == {"add", "use", "list", "remove", "status"}


@pytest.mark.parametrize("command", ["add", "use", "remove"])
def test_build_parser_requires_alias_for_mutating_commands(command):
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([command])


def test_main_returns_zero_for_valid_command():
    assert main(["status"]) == 0
