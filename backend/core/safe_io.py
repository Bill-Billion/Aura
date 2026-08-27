"""Small no-symlink, bounded readers for research and run artifacts."""

from __future__ import annotations

import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_parent_directory(path: Path | str, *, create: bool) -> tuple[int, Path]:
    absolute = Path(os.path.abspath(Path(path)))
    if absolute.name in {"", ".", ".."}:
        raise ValueError(f"invalid artifact path: {path}")
    directory_fd = os.open(os.path.sep, _DIRECTORY_FLAGS)
    try:
        for component in absolute.parent.parts[1:]:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
            child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
    except BaseException:
        os.close(directory_fd)
        raise
    return directory_fd, absolute


def read_bounded_regular_file(path: Path | str, *, max_bytes: int) -> bytes:
    """Read one regular file without following any path-component symlink."""

    directory_fd, absolute = _open_parent_directory(path, create=False)
    try:
        descriptor = os.open(
            absolute.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"artifact is not a regular file: {path}")
            if metadata.st_size > max_bytes:
                raise ValueError(f"artifact exceeds {max_bytes} bytes: {path}")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            encoded = b"".join(chunks)
            if len(encoded) > max_bytes:
                raise ValueError(f"artifact exceeds {max_bytes} bytes: {path}")
            return encoded
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)


@contextmanager
def open_private_append(path: Path | str) -> Iterator[BinaryIO]:
    """Append to a private regular file without following path symlinks."""

    directory_fd, absolute = _open_parent_directory(path, create=True)
    try:
        descriptor = os.open(
            absolute.name,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"artifact is not a regular file: {path}")
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "ab", closefd=False) as handle:
                yield handle
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)


def atomic_replace_private_file(path: Path | str, payload: bytes) -> Path:
    """Atomically replace one private file through a no-symlink directory fd."""

    directory_fd, absolute = _open_parent_directory(path, create=True)
    temp_name = f".{absolute.name}.{secrets.token_hex(8)}.tmp"
    try:
        descriptor = os.open(
            temp_name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(
            temp_name,
            absolute.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
        return absolute
    except BaseException:
        try:
            os.unlink(temp_name, dir_fd=directory_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(directory_fd)


__all__ = [
    "atomic_replace_private_file",
    "open_private_append",
    "read_bounded_regular_file",
]
