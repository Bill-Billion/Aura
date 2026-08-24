"""Atomic and integrity-checked experiment artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .spec import (
    RESOLVED_MATRIX_SCHEMA_VERSION,
    ExperimentCell,
    ResolvedMatrix,
    canonical_json,
    sha256_json,
)

RESOLVED_MATRIX_FILENAME = "resolved-matrix.json"
RESULT_FILENAME = "result.json"
MAX_RESOLVED_MATRIX_BYTES = 32 * 1024 * 1024
MAX_CELL_RESULT_BYTES = 64 * 1024 * 1024
_CELL_ID_RE = re.compile(r"^cell-[0-9a-f]{32}$")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactSeal(_StrictModel):
    algorithm: Literal["sha256"] = "sha256"
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CellError(_StrictModel):
    type: str
    message: str


class CellResult(_StrictModel):
    result_schema_version: Literal["1.0"] = "1.0"
    cell_id: str
    cell_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    matrix_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["completed", "evaluation_error", "invalid_evidence", "failed"]
    output: dict[str, Any] | None = None
    error: CellError | None = None

    @model_validator(mode="after")
    def _status_shape(self) -> "CellResult":
        if self.status in {"completed", "evaluation_error", "invalid_evidence"}:
            if self.output is None or self.error is not None:
                raise ValueError(
                    f"{self.status} cell result requires output and forbids error"
                )
        elif self.error is None:
            raise ValueError("failed cell result requires error")
        return self


class CellResultArtifact(_StrictModel):
    result: CellResult
    seal: ArtifactSeal

    @classmethod
    def completed(
        cls,
        *,
        cell: ExperimentCell,
        matrix_hash: str,
        output: dict[str, Any],
    ) -> "CellResultArtifact":
        result = CellResult(
            cell_id=cell.cell_id,
            cell_hash=cell.contract_hash(),
            matrix_hash=matrix_hash,
            status="completed",
            output=output,
        )
        return cls(
            result=result,
            seal=ArtifactSeal(sha256=sha256_json(result.model_dump(mode="json"))),
        )

    @classmethod
    def failed(
        cls,
        *,
        cell: ExperimentCell,
        matrix_hash: str,
        exc: BaseException,
    ) -> "CellResultArtifact":
        result = CellResult(
            cell_id=cell.cell_id,
            cell_hash=cell.contract_hash(),
            matrix_hash=matrix_hash,
            status="failed",
            error=CellError(type=type(exc).__name__, message=str(exc)),
        )
        return cls(
            result=result,
            seal=ArtifactSeal(sha256=sha256_json(result.model_dump(mode="json"))),
        )

    @classmethod
    def evaluation_error(
        cls,
        *,
        cell: ExperimentCell,
        matrix_hash: str,
        output: dict[str, Any],
    ) -> "CellResultArtifact":
        result = CellResult(
            cell_id=cell.cell_id,
            cell_hash=cell.contract_hash(),
            matrix_hash=matrix_hash,
            status="evaluation_error",
            output=output,
        )
        return cls(
            result=result,
            seal=ArtifactSeal(sha256=sha256_json(result.model_dump(mode="json"))),
        )

    @classmethod
    def invalid_evidence(
        cls,
        *,
        cell: ExperimentCell,
        matrix_hash: str,
        output: dict[str, Any],
    ) -> "CellResultArtifact":
        result = CellResult(
            cell_id=cell.cell_id,
            cell_hash=cell.contract_hash(),
            matrix_hash=matrix_hash,
            status="invalid_evidence",
            output=output,
        )
        return cls(
            result=result,
            seal=ArtifactSeal(sha256=sha256_json(result.model_dump(mode="json"))),
        )

    @model_validator(mode="after")
    def _verify_seal(self) -> "CellResultArtifact":
        expected = sha256_json(self.result.model_dump(mode="json"))
        if self.seal.sha256 != expected:
            raise ValueError("cell result seal does not match its contents")
        return self

    def is_completed_for(
        self,
        *,
        cell: ExperimentCell,
        matrix_hash: str,
    ) -> bool:
        return (
            self.result.status == "completed"
            and self.result.cell_id == cell.cell_id
            and self.result.cell_hash == cell.contract_hash()
            and self.result.matrix_hash == matrix_hash
        )


def atomic_write_json(path: Path | str, value: Any) -> Path:
    """Write canonical JSON through a same-directory rename."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (canonical_json(value) + "\n").encode("utf-8")
    descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return path


def _atomic_create_bytes(path: Path, encoded: bytes) -> Path:
    """Atomically create *path* without replacing existing evidence."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    return path


def atomic_create_bytes(
    path: Path | str,
    encoded: bytes,
    *,
    max_bytes: int,
) -> Path:
    """Create immutable evidence, accepting an existing byte-identical file."""

    path = Path(path)
    if len(encoded) > max_bytes:
        raise ValueError(f"artifact exceeds {max_bytes} bytes: {path}")
    absolute_parent = Path(os.path.abspath(path.parent))
    directory_fd = os.open(os.path.sep, _DIRECTORY_FLAGS)
    try:
        for component in absolute_parent.parts[1:]:
            try:
                os.mkdir(component, mode=0o700, dir_fd=directory_fd)
            except FileExistsError:
                pass
            child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        try:
            _atomic_create_bytes_at(directory_fd, path.name, encoded)
        except FileExistsError:
            existing, _ = _read_regular_file_at(
                directory_fd, path.name, max_bytes=max_bytes
            )
            if existing != encoded:
                raise ValueError(
                    f"artifact already exists with different contents: {path}"
                )
    finally:
        os.close(directory_fd)
    return path


def _atomic_create_bytes_at(directory_fd: int, name: str, encoded: bytes) -> None:
    temp_name = f".{name}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(
        temp_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(
            temp_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
    finally:
        try:
            os.unlink(temp_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _open_child_directory(
    parent_fd: int,
    name: str,
    *,
    create: bool,
) -> int:
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)


@contextmanager
def _cell_directory_fd(
    root: Path | str,
    cell_id: str,
    *,
    create: bool,
):
    if _CELL_ID_RE.fullmatch(cell_id) is None:
        raise ValueError(f"invalid cell id: {cell_id!r}")
    root_path = Path(root)
    if create:
        root_path.mkdir(parents=True, exist_ok=True)
    root_fd = os.open(root_path, _DIRECTORY_FLAGS)
    cells_fd: int | None = None
    cell_fd: int | None = None
    try:
        cells_fd = _open_child_directory(root_fd, "cells", create=create)
        cell_fd = _open_child_directory(cells_fd, cell_id, create=create)
        yield cell_fd
    finally:
        if cell_fd is not None:
            os.close(cell_fd)
        if cells_fd is not None:
            os.close(cells_fd)
        os.close(root_fd)


def _read_regular_file_at(
    directory_fd: int,
    name: str,
    *,
    max_bytes: int,
) -> tuple[bytes, os.stat_result]:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"artifact is not a regular file: {name}")
        if metadata.st_size > max_bytes:
            raise ValueError(f"artifact exceeds {max_bytes} bytes: {name}")
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
            raise ValueError(f"artifact exceeds {max_bytes} bytes: {name}")
        return encoded, metadata
    finally:
        os.close(descriptor)


def atomic_create_json(
    path: Path | str,
    value: Any,
    *,
    max_bytes: int | None = None,
) -> Path:
    encoded = (canonical_json(value) + "\n").encode("utf-8")
    if max_bytes is not None and len(encoded) > max_bytes:
        raise ValueError(f"JSON artifact exceeds {max_bytes} bytes: {path}")
    return _atomic_create_bytes(Path(path), encoded)


def cell_result_exists(root: Path | str, cell_id: str) -> bool:
    try:
        with _cell_directory_fd(root, cell_id, create=False) as directory_fd:
            os.stat(RESULT_FILENAME, dir_fd=directory_fd, follow_symlinks=False)
            return True
    except FileNotFoundError:
        return False


def archive_cell_result(root: Path | str, cell_id: str) -> Path:
    """Move an invalid or retried result into append-only, content-addressed history."""

    with _cell_directory_fd(root, cell_id, create=False) as cell_fd:
        encoded, original = _read_regular_file_at(
            cell_fd,
            RESULT_FILENAME,
            max_bytes=MAX_CELL_RESULT_BYTES,
        )
        digest = hashlib.sha256(encoded).hexdigest()
        archive_name = f"result-{digest}.json"
        attempts_fd = _open_child_directory(cell_fd, "attempts", create=True)
        try:
            try:
                _atomic_create_bytes_at(attempts_fd, archive_name, encoded)
            except FileExistsError:
                archived, _ = _read_regular_file_at(
                    attempts_fd,
                    archive_name,
                    max_bytes=MAX_CELL_RESULT_BYTES,
                )
                if archived != encoded:
                    raise RuntimeError(
                        f"cell attempt archive hash collision: {archive_name}"
                    )
        finally:
            os.close(attempts_fd)

        current, current_metadata = _read_regular_file_at(
            cell_fd,
            RESULT_FILENAME,
            max_bytes=MAX_CELL_RESULT_BYTES,
        )
        identity = (original.st_dev, original.st_ino, original.st_size)
        current_identity = (
            current_metadata.st_dev,
            current_metadata.st_ino,
            current_metadata.st_size,
        )
        if current != encoded or current_identity != identity:
            raise RuntimeError("cell result changed while it was being archived")
        os.unlink(RESULT_FILENAME, dir_fd=cell_fd)
    return cell_result_path(root, cell_id).parent / "attempts" / archive_name


def resolved_matrix_path(root: Path | str) -> Path:
    return Path(root) / RESOLVED_MATRIX_FILENAME


def cell_result_path(root: Path | str, cell_id: str) -> Path:
    return Path(root) / "cells" / cell_id / RESULT_FILENAME


def write_resolved_matrix(root: Path | str, matrix: ResolvedMatrix) -> Path:
    if matrix.matrix_schema_version != RESOLVED_MATRIX_SCHEMA_VERSION:
        raise ValueError(
            f"resolved matrix {matrix.matrix_schema_version} is read-only; "
            f"only schema {RESOLVED_MATRIX_SCHEMA_VERSION} may be written"
        )
    path = resolved_matrix_path(root)
    try:
        return atomic_create_json(
            path,
            matrix.model_dump(mode="json"),
            max_bytes=MAX_RESOLVED_MATRIX_BYTES,
        )
    except FileExistsError:
        existing = read_resolved_matrix(path)
        if existing.matrix_hash != matrix.matrix_hash:
            raise ValueError(
                f"output directory already belongs to matrix {existing.matrix_hash}, "
                f"not {matrix.matrix_hash}"
            )
        return path


def read_resolved_matrix(path: Path | str) -> ResolvedMatrix:
    path = Path(path)
    try:
        if path.stat().st_size > MAX_RESOLVED_MATRIX_BYTES:
            raise ValueError(
                f"resolved matrix exceeds {MAX_RESOLVED_MATRIX_BYTES} bytes: {path}"
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read resolved matrix {path}: {exc}") from exc
    return ResolvedMatrix.model_validate(raw)


def write_cell_result(
    root: Path | str,
    artifact: CellResultArtifact,
) -> Path:
    encoded = (
        canonical_json(artifact.model_dump(mode="json")) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_CELL_RESULT_BYTES:
        raise ValueError(
            f"cell result exceeds {MAX_CELL_RESULT_BYTES} bytes: "
            f"{artifact.result.cell_id}"
        )
    with _cell_directory_fd(root, artifact.result.cell_id, create=True) as directory_fd:
        _atomic_create_bytes_at(directory_fd, RESULT_FILENAME, encoded)
    return cell_result_path(root, artifact.result.cell_id)


def read_cell_result_at(root: Path | str, cell_id: str) -> CellResultArtifact:
    try:
        with _cell_directory_fd(root, cell_id, create=False) as directory_fd:
            encoded, _ = _read_regular_file_at(
                directory_fd,
                RESULT_FILENAME,
                max_bytes=MAX_CELL_RESULT_BYTES,
            )
        raw = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read cell result {cell_id}: {exc}") from exc
    return CellResultArtifact.model_validate(raw)


def read_cell_result(path: Path | str) -> CellResultArtifact:
    path = Path(path)
    try:
        if path.stat().st_size > MAX_CELL_RESULT_BYTES:
            raise ValueError(
                f"cell result exceeds {MAX_CELL_RESULT_BYTES} bytes: {path}"
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read cell result {path}: {exc}") from exc
    return CellResultArtifact.model_validate(raw)


__all__ = [
    "RESOLVED_MATRIX_FILENAME",
    "RESULT_FILENAME",
    "ArtifactSeal",
    "CellError",
    "CellResult",
    "CellResultArtifact",
    "MAX_CELL_RESULT_BYTES",
    "MAX_RESOLVED_MATRIX_BYTES",
    "archive_cell_result",
    "atomic_create_bytes",
    "atomic_create_json",
    "atomic_write_json",
    "cell_result_exists",
    "cell_result_path",
    "read_cell_result",
    "read_cell_result_at",
    "read_resolved_matrix",
    "resolved_matrix_path",
    "write_cell_result",
    "write_resolved_matrix",
]
