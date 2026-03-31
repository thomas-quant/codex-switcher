from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from codex_switch.errors import StateFileError
from codex_switch.fs import atomic_write_bytes
from codex_switch.models import AppState


class StateStore:
    def __init__(self, state_file: Path) -> None:
        self._state_file = state_file

    def load(self) -> AppState:
        if not self._state_file.exists():
            return AppState()

        try:
            raw = self._state_file.read_bytes()
        except OSError as exc:
            raise StateFileError(f"Could not read {self._state_file}: {exc}") from exc
        try:
            text = raw.decode("utf-8")
            payload = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StateFileError(f"Could not parse {self._state_file}: {exc}") from exc

        if not isinstance(payload, dict):
            raise StateFileError(f"Could not parse {self._state_file}: expected a JSON object")

        version = payload.get("version", 1)
        active_alias = payload.get("active_alias")
        updated_at = payload.get("updated_at")

        if type(version) is not int:
            raise StateFileError(f"Could not parse {self._state_file}: version must be an integer")
        if version != 1:
            raise StateFileError(f"Could not parse {self._state_file}: unsupported schema version {version}")
        if active_alias is not None and not isinstance(active_alias, str):
            raise StateFileError(f"Could not parse {self._state_file}: active_alias must be a string or null")
        if updated_at is not None and not isinstance(updated_at, str):
            raise StateFileError(f"Could not parse {self._state_file}: updated_at must be a string or null")

        return AppState(
            version=version,
            active_alias=active_alias,
            updated_at=updated_at,
        )

    def save(self, state: AppState) -> None:
        body = json.dumps(asdict(state), indent=2, sort_keys=True).encode("utf-8") + b"\n"
        atomic_write_bytes(self._state_file, body, mode=0o600, root=self._state_file.parent)
