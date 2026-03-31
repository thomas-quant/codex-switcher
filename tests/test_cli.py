from codex_switch.cli import build_parser


def test_build_parser_registers_expected_subcommands():
    parser = build_parser()
    subparsers = next(action for action in parser._actions if getattr(action, "choices", None))
    assert set(subparsers.choices) == {"add", "use", "list", "remove", "status"}
