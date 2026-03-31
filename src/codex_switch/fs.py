from __future__ import annotations

import errno
import hashlib
import os
import tempfile
from pathlib import Path


def ensure_private_dir(path: Path, mode: int = 0o700, root: Path | None = None) -> None:
    path = Path(path)

    if root is not None:
        root = Path(root)
        if root.exists() and root.is_symlink():
            raise ValueError(f"{root} is a symlink")
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"{path} is not under {root}") from exc

        resolved_root = root.resolve(strict=False)
        resolved_path = path.resolve(strict=False)
        if not resolved_path.is_relative_to(resolved_root):
            raise ValueError(f"{path} escapes {root} via symlink")

        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, mode)
        current = root
        for part in relative.parts:
            current = current / part
            if not current.exists():
                current.mkdir(exist_ok=True)
            if current.is_symlink():
                raise ValueError(f"{current} is a symlink")
            os.chmod(current, mode)
        return

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


def atomic_write_bytes(
    target: Path,
    data: bytes,
    mode: int = 0o600,
    root: Path | None = None,
) -> None:
    ensure_private_dir(target.parent, root=root)
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


def atomic_copy_file(
    source: Path,
    target: Path,
    mode: int = 0o600,
    root: Path | None = None,
) -> None:
    atomic_write_bytes(target, source.read_bytes(), mode=mode, root=root)


def file_digest(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()
