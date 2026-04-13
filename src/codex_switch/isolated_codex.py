from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from codex_switch.fs import atomic_write_bytes


@contextmanager
def isolated_codex_env(auth_bytes: bytes | None = None) -> Iterator[dict[str, str]]:
    with tempfile.TemporaryDirectory(prefix="codex-switch-isolated-") as raw_home:
        home = Path(raw_home)
        codex_root = home / ".codex"
        if auth_bytes is not None:
            atomic_write_bytes(
                codex_root / "auth.json",
                auth_bytes,
                mode=0o600,
                root=home,
            )
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["CODEX_HOME"] = str(codex_root)
        yield env
