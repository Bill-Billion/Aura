"""Small no-symlink, bounded readers for research and run artifacts."""

from __future__ import annotations

import os
import stat
from pathlib import Path


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def read_bounded_regular_file(path: Path | str, *, max_bytes: int) -> bytes:
    """Read one regular file without following any path-component symlink."""

    absolute = Path(os.path.abspath(Path(path)))
    if absolute.name in {"", ".", ".."}:
        raise ValueError(f"invalid artifact path: {path}")
    directory_fd = os.open(os.path.sep, _DIRECTORY_FLAGS)
    try:
        for component in absolute.parent.parts[1:]:
            child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
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


__all__ = ["read_bounded_regular_file"]
