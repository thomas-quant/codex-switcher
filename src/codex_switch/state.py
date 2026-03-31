from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from codex_switch.errors import StateFileError
from codex_switch.fs import atomic_write_bytes, ensure_private_dir
from codex_switch.models import AppState


class StateStore:
    def __init__(self, state_file: Path) -> None:
        self._state_file = state_file

    def load(self) -> AppState:
        if not self._state_file.exists():
            return AppState()

        try:
            payload = json.loads(self._state_file.read_text())
        except json.JSONDecodeError as exc:
            raise StateFileError(f"Could not parse {self._state_file}") from exc

        return AppState(
            version=payload.get("version", 1),
            active_alias=payload.get("active_alias"),
            updated_at=payload.get("updated_at"),
        )

    def save(self, state: AppState) -> None:
        ensure_private_dir(self._state_file.parent)
        body = json.dumps(asdict(state), indent=2, sort_keys=True).encode("utf-8") + b"\n"
        atomic_write_bytes(self._state_file, body, mode=0o600)
