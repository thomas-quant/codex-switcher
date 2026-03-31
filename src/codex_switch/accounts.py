from __future__ import annotations

import re
from pathlib import Path

from codex_switch.errors import (
    AliasAlreadyExistsError,
    InvalidAliasError,
    SnapshotNotFoundError,
    UnsafeSnapshotEntryError,
)
from codex_switch.fs import atomic_copy_file, atomic_write_bytes, ensure_private_dir

ALIAS_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


class AccountStore:
    def __init__(self, accounts_dir: Path) -> None:
        self._accounts_dir = accounts_dir

    def _root(self) -> Path:
        return self._accounts_dir.parent

    def _safe_accounts_dir(self) -> Path:
        root = self._root()
        if root.exists() and root.is_symlink():
            raise ValueError(f"{root} is a symlink")
        try:
            self._accounts_dir.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"{self._accounts_dir} is not under {root}") from exc

        resolved_root = root.resolve(strict=False)
        resolved_path = self._accounts_dir.resolve(strict=False)
        if not resolved_path.is_relative_to(resolved_root):
            raise ValueError(f"{self._accounts_dir} escapes {root} via symlink")
        if self._accounts_dir.exists() and self._accounts_dir.is_symlink():
            raise ValueError(f"{self._accounts_dir} is a symlink")
        return self._accounts_dir

    def _validate_alias(self, alias: str) -> None:
        if not ALIAS_RE.fullmatch(alias):
            raise InvalidAliasError(
                "Alias must match ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$"
            )

    def _safe_snapshot_entry(self, alias: str) -> Path:
        path = self.snapshot_path(alias)
        if path.is_symlink():
            raise UnsafeSnapshotEntryError(f"Unsafe snapshot entry: {path}")
        if not path.exists():
            return path
        if not path.is_file():
            raise UnsafeSnapshotEntryError(f"Unsafe snapshot entry: {path}")
        return path

    def snapshot_path(self, alias: str) -> Path:
        self._validate_alias(alias)
        return self._safe_accounts_dir() / f"{alias}.json"

    def exists(self, alias: str) -> bool:
        path = self._safe_snapshot_entry(alias)
        return path.exists()

    def list_aliases(self) -> list[str]:
        accounts_dir = self._safe_accounts_dir()
        if not accounts_dir.exists():
            return []
        aliases: list[str] = []
        for path in sorted(accounts_dir.glob("*.json")):
            alias = path.stem
            if not ALIAS_RE.fullmatch(alias):
                raise InvalidAliasError(f"Malformed snapshot filename: {path.name}")
            if path.is_symlink() or not path.exists() or not path.is_file():
                raise UnsafeSnapshotEntryError(f"Unsafe snapshot entry: {path}")
            aliases.append(alias)
        return aliases

    def write_snapshot_from_file(self, alias: str, source: Path) -> None:
        target = self.snapshot_path(alias)
        root = self._root()
        ensure_private_dir(self._safe_accounts_dir(), root=root)
        atomic_copy_file(source, target, mode=0o600, root=root)

    def write_snapshot_from_bytes(self, alias: str, payload: bytes) -> None:
        target = self.snapshot_path(alias)
        root = self._root()
        ensure_private_dir(self._safe_accounts_dir(), root=root)
        atomic_write_bytes(target, payload, mode=0o600, root=root)

    def read_snapshot(self, alias: str) -> bytes:
        path = self._safe_snapshot_entry(alias)
        if not path.exists():
            raise SnapshotNotFoundError(f"Alias '{alias}' does not exist")
        return path.read_bytes()

    def delete(self, alias: str) -> None:
        path = self._safe_snapshot_entry(alias)
        if not path.exists():
            raise SnapshotNotFoundError(f"Alias '{alias}' does not exist")
        path.unlink()

    def assert_missing(self, alias: str) -> None:
        if self.exists(alias):
            raise AliasAlreadyExistsError(f"Alias '{alias}' already exists")
