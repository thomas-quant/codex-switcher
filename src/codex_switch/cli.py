from __future__ import annotations

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-switch")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("add", "use", "list", "remove", "status"):
        child = subparsers.add_parser(name)
        if name in {"add", "use", "remove"}:
            child.add_argument("alias")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(list(argv) if argv is not None else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
