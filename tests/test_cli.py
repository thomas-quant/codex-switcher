import pytest

from codex_switch.errors import CodexSwitchError
from codex_switch.models import StatusResult
from codex_switch.cli import build_parser
from codex_switch.cli import format_alias_lines
from codex_switch.cli import format_status_lines
from codex_switch.cli import main


def test_build_parser_registers_expected_subcommands():
    parser = build_parser()
    subparsers = next(action for action in parser._actions if getattr(action, "choices", None))
    assert set(subparsers.choices) == {"add", "use", "list", "remove", "status"}


def test_build_parser_add_includes_alias_argument():
    parser = build_parser()

    namespace = parser.parse_args(["add", "work"])

    assert namespace.command == "add"
    assert namespace.alias == "work"


@pytest.mark.parametrize("command", ["add", "use", "remove"])
def test_build_parser_requires_alias_for_mutating_commands(command):
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([command])


def test_build_parser_list_takes_no_alias_argument():
    parser = build_parser()

    namespace = parser.parse_args(["list"])

    assert namespace.command == "list"
    assert not hasattr(namespace, "alias")


def test_format_alias_lines_marks_active_alias():
    assert format_alias_lines(["personal", "work"], "work") == [
        "  personal",
        "* work",
    ]


def test_format_alias_lines_handles_empty_alias_list():
    assert format_alias_lines([], None) == ["No aliases configured."]


def test_format_status_lines_marks_dirty_state():
    status = StatusResult(
        active_alias="work",
        snapshot_exists=True,
        live_auth_exists=True,
        in_sync=False,
    )

    assert format_status_lines(status) == [
        "active alias: work",
        "snapshot: present",
        "live auth: present",
        "sync: dirty",
    ]


def test_format_status_lines_with_no_active_alias_is_two_lines():
    status = StatusResult(
        active_alias=None,
        snapshot_exists=False,
        live_auth_exists=True,
        in_sync=None,
    )

    assert format_status_lines(status) == [
        "active alias: none",
        "live auth: present",
    ]


def test_main_dispatches_add(monkeypatch, capsys):
    calls: list[tuple[str, str | None]] = []

    class FakeManager:
        def add(self, alias: str) -> None:
            calls.append(("add", alias))

    monkeypatch.setattr("codex_switch.cli.build_default_manager", lambda: FakeManager())

    result = main(["add", "work"])

    captured = capsys.readouterr()
    assert result == 0
    assert calls == [("add", "work")]
    assert captured.out == "added alias: work\n"


def test_main_dispatches_use(monkeypatch, capsys):
    calls: list[tuple[str, str | None]] = []

    class FakeManager:
        def use(self, alias: str) -> None:
            calls.append(("use", alias))

    monkeypatch.setattr("codex_switch.cli.build_default_manager", lambda: FakeManager())

    result = main(["use", "work"])

    captured = capsys.readouterr()
    assert result == 0
    assert calls == [("use", "work")]
    assert captured.out == "active alias: work\n"


def test_main_dispatches_list(monkeypatch, capsys):
    class FakeManager:
        def list_aliases(self) -> tuple[list[str], str | None]:
            return ["personal", "work"], "work"

    monkeypatch.setattr("codex_switch.cli.build_default_manager", lambda: FakeManager())

    result = main(["list"])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out.splitlines() == ["  personal", "* work"]


def test_main_dispatches_remove(monkeypatch, capsys):
    calls: list[tuple[str, str | None]] = []

    class FakeManager:
        def remove(self, alias: str) -> None:
            calls.append(("remove", alias))

    monkeypatch.setattr("codex_switch.cli.build_default_manager", lambda: FakeManager())

    result = main(["remove", "work"])

    captured = capsys.readouterr()
    assert result == 0
    assert calls == [("remove", "work")]
    assert captured.out == "removed alias: work\n"


def test_main_dispatches_status(monkeypatch, capsys):
    class FakeManager:
        def status(self) -> StatusResult:
            return StatusResult(
                active_alias=None,
                snapshot_exists=False,
                live_auth_exists=True,
                in_sync=None,
            )

    monkeypatch.setattr("codex_switch.cli.build_default_manager", lambda: FakeManager())

    result = main(["status"])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out.splitlines() == [
        "active alias: none",
        "live auth: present",
    ]


def test_main_exits_via_parser_for_user_facing_errors(monkeypatch):
    class FakeManager:
        def add(self, alias: str) -> None:
            raise CodexSwitchError(f"bad alias: {alias}")

    monkeypatch.setattr("codex_switch.cli.build_default_manager", lambda: FakeManager())

    with pytest.raises(SystemExit) as exc_info:
        main(["add", "broken"])

    assert exc_info.value.code == 1
