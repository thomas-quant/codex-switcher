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
    temp_path: Path | None = None
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
        temp_path = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp_path, mode)
    try:
        os.replace(temp_path, target)
        try:
            dir_fd = os.open(
                target.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
        except OSError:
            pass
        else:
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
            finally:
                os.close(dir_fd)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def atomic_copy_file(source: Path, target: Path, mode: int = 0o600) -> None:
    atomic_write_bytes(target, source.read_bytes(), mode=mode)


def file_digest(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()
