from __future__ import annotations

import errno
import hashlib
import os
import tempfile
from pathlib import Path


def ensure_private_dir(path: Path, mode: int = 0o700) -> None:
    path = Path(path)
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent

    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        os.chmod(directory, mode)

    if path.exists():
        os.chmod(path, mode)


_DIR_FSYNC_UNSUPPORTED_ERRNOS = {
    errno.EBADF,
    errno.EINVAL,
    errno.EISDIR,
    errno.ENOTDIR,
    errno.ENOTSUP,
    errno.EOPNOTSUPP,
}


def _fsync_directory(path: Path) -> None:
    try:
        dir_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        if exc.errno in _DIR_FSYNC_UNSUPPORTED_ERRNOS:
            return
        raise

    try:
        os.fsync(dir_fd)
    except OSError as exc:
        if exc.errno in _DIR_FSYNC_UNSUPPORTED_ERRNOS:
            return
        raise
    finally:
        os.close(dir_fd)


def atomic_write_bytes(target: Path, data: bytes, mode: int = 0o600) -> None:
    ensure_private_dir(target.parent)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, target)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise
    _fsync_directory(target.parent)


def atomic_copy_file(source: Path, target: Path, mode: int = 0o600) -> None:
    atomic_write_bytes(target, source.read_bytes(), mode=mode)


def file_digest(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()
