from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


def ensure_private_dir(path: Path, mode: int = 0o700) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)


def atomic_write_bytes(target: Path, data: bytes, mode: int = 0o600) -> None:
    ensure_private_dir(target.parent)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
        handle.write(data)
        temp_path = Path(handle.name)
    os.chmod(temp_path, mode)
    os.replace(temp_path, target)


def atomic_copy_file(source: Path, target: Path, mode: int = 0o600) -> None:
    atomic_write_bytes(target, source.read_bytes(), mode=mode)


def file_digest(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()
