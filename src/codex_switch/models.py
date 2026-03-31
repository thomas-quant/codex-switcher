from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class AppPaths:
    home: Path
    codex_root: Path
    live_auth_file: Path
    switch_root: Path
    accounts_dir: Path
    state_file: Path


@dataclass(slots=True, frozen=True)
class AppState:
    version: int = 1
    active_alias: str | None = None
    updated_at: str | None = None


@dataclass(slots=True, frozen=True)
class StatusResult:
    active_alias: str | None
    snapshot_exists: bool
    live_auth_exists: bool
    in_sync: bool | None
