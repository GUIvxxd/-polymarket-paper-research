#!/usr/bin/env python3
"""Root-isolated path and exact-byte primitives for future X v5 evidence."""
from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_DESCRIPTOR_WALK_SUPPORTED = (
    os.open in os.supports_dir_fd
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
)
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


class RootIsolationError(ValueError):
    pass


class ArtifactIntegrityError(ValueError):
    pass


class SourceIntegrityError(ValueError):
    pass


def _absolute_root_parts(root: Path) -> tuple[Path, tuple[str, ...]]:
    if not isinstance(root, Path):
        raise RootIsolationError("root must be an explicit Path")
    if not root.is_absolute():
        raise RootIsolationError("root must be absolute")
    if not _SAFE_DESCRIPTOR_WALK_SUPPORTED or root.anchor != os.sep:
        raise RootIsolationError(
            "platform cannot provide a root-local no-follow descriptor walk"
        )
    parts = root.parts[1:]
    if any(part in {"", ".", ".."} for part in parts):
        raise RootIsolationError("root path traversal is not allowed")
    return root.absolute(), parts


def _open_child_fd(name: str, flags: int, *, dir_fd: int) -> int:
    return os.open(name, flags, dir_fd=dir_fd)


def _open_root_path_fd(root: Path) -> tuple[Path, int]:
    absolute_root, parts = _absolute_root_parts(root)
    current_fd = -1
    try:
        current_fd = os.open(absolute_root.anchor, _DIRECTORY_OPEN_FLAGS)
        for part in parts:
            child_fd = _open_child_fd(
                part,
                _DIRECTORY_OPEN_FLAGS,
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = child_fd
    except OSError as exc:
        if current_fd >= 0:
            os.close(current_fd)
        raise RootIsolationError(
            "root must be an existing directory with no symlink components"
        ) from exc
    return absolute_root, current_fd


def _explicit_root(root: Path) -> tuple[Path, tuple[int, int]]:
    absolute_root, root_fd = _open_root_path_fd(root)
    try:
        root_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise RootIsolationError("root must be an existing directory")
        identity = (root_stat.st_dev, root_stat.st_ino)
    finally:
        os.close(root_fd)
    return absolute_root, identity


def _relative_parts(relative_path: str | Path) -> tuple[str, ...]:
    if isinstance(relative_path, Path) and relative_path.is_absolute():
        raise RootIsolationError("path must be root-relative")
    raw = str(relative_path)
    if not raw or "\\" in raw:
        raise RootIsolationError("path must use a non-empty root-relative POSIX form")
    parsed = PurePosixPath(raw)
    if parsed.is_absolute() or parsed == PurePosixPath("."):
        raise RootIsolationError("path must be root-relative")
    if any(part in {"", ".", ".."} for part in parsed.parts):
        raise RootIsolationError("path traversal is not allowed")
    if parsed.parts[0].endswith(":"):
        raise RootIsolationError("drive-qualified paths are not allowed")
    return parsed.parts


def _resolve_under_root(root: Path, relative_path: str | Path) -> Path:
    candidate = root
    for part in _relative_parts(relative_path):
        candidate = candidate / part
        if candidate.is_symlink():
            raise RootIsolationError("symlink paths are not allowed")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RootIsolationError("path escapes the explicit root") from exc
    return resolved


@dataclass(frozen=True)
class V5PathBundle:
    root: Path
    root_device: int
    root_inode: int
    config: Path
    output: Path
    raw: Path
    proofs: Path
    settlement_proofs: Path
    protocol: Path
    lock: Path
    state: Path
    status: Path
    registry: Path
    events: Path
    ledger: Path
    audit: Path

    @classmethod
    def from_root(cls, root: Path) -> "V5PathBundle":
        explicit_root, root_identity = _explicit_root(root)
        output = explicit_root / "reports" / "xtracker_forward_validation" / "v5"
        return cls(
            root=explicit_root,
            root_device=root_identity[0],
            root_inode=root_identity[1],
            config=explicit_root / "config",
            output=output,
            raw=output / "raw",
            proofs=output / "proofs",
            settlement_proofs=output / "settlement_proofs",
            protocol=output / "protocol.json",
            lock=output / "lock.json",
            state=output / "state.json",
            status=output / "status.json",
            registry=output / "opportunity_registry.jsonl",
            events=output / "evidence_events.jsonl",
            ledger=output / "independent_event_ledger.csv",
            audit=output / "audit.json",
        )

    def resolve_relative(self, relative_path: str | Path) -> Path:
        return _resolve_under_root(self.root, relative_path)


def _open_verified_root_fd(paths: V5PathBundle) -> int:
    _, root_fd = _open_root_path_fd(paths.root)
    try:
        root_stat = os.fstat(root_fd)
        if (root_stat.st_dev, root_stat.st_ino) != (
            paths.root_device,
            paths.root_inode,
        ):
            raise RootIsolationError("explicit root identity changed after validation")
    except BaseException:
        os.close(root_fd)
        raise
    return root_fd


def _read_root_local_bytes(
    paths: V5PathBundle,
    relative_path: str | Path,
    *,
    label: str,
    error_type: type[ValueError],
) -> bytes:
    parts = _relative_parts(relative_path)
    parent_fd = -1
    file_fd = -1
    try:
        parent_fd = _open_verified_root_fd(paths)
        for part in parts[:-1]:
            try:
                child_fd = _open_child_fd(
                    part,
                    _DIRECTORY_OPEN_FLAGS,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                raise
            except OSError as exc:
                raise RootIsolationError(
                    "symlink or non-directory path components are not allowed"
                ) from exc
            os.close(parent_fd)
            parent_fd = child_fd
        file_fd = _open_child_fd(
            parts[-1],
            _FILE_OPEN_FLAGS,
            dir_fd=parent_fd,
        )
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise error_type(
                f"{label} is not a regular file opened through a "
                f"safe root-local descriptor: {relative_path}"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
        after = os.fstat(file_fd)
        if (
            (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or len(data) != after.st_size
        ):
            raise error_type(
                f"{label} changed while reading from its safe "
                f"root-local descriptor: {relative_path}"
            )
        return data
    except RootIsolationError:
        raise
    except FileNotFoundError as exc:
        raise error_type(
            f"{label} is missing under the explicit root: {relative_path}"
        ) from exc
    except OSError as exc:
        raise error_type(
            f"{label} cannot be read through a safe "
            f"root-local descriptor: {relative_path}"
        ) from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


@dataclass(frozen=True)
class ArtifactReference:
    role: str
    relative_path: str
    sha256: str
    byte_length: int
    source_type: str

    def __post_init__(self) -> None:
        if not self.role:
            raise ArtifactIntegrityError("artifact role is required")
        if not _SHA256_PATTERN.fullmatch(self.sha256):
            raise ArtifactIntegrityError("artifact SHA-256 must be lowercase hexadecimal")
        if self.byte_length < 0:
            raise ArtifactIntegrityError("artifact byte length cannot be negative")
        if not self.source_type:
            raise ArtifactIntegrityError("artifact source type is required")

    @classmethod
    def from_bytes(
        cls,
        *,
        role: str,
        relative_path: str,
        data: bytes,
        source_type: str,
    ) -> "ArtifactReference":
        return cls(
            role=role,
            relative_path=relative_path,
            sha256=hashlib.sha256(data).hexdigest(),
            byte_length=len(data),
            source_type=source_type,
        )


def read_verified_artifact(
    paths: V5PathBundle,
    reference: ArtifactReference,
) -> bytes:
    data = _read_root_local_bytes(
        paths,
        reference.relative_path,
        label="artifact",
        error_type=ArtifactIntegrityError,
    )
    if len(data) != reference.byte_length:
        raise ArtifactIntegrityError(
            f"artifact byte length mismatch: {reference.relative_path}"
        )
    if hashlib.sha256(data).hexdigest() != reference.sha256:
        raise ArtifactIntegrityError(f"artifact SHA-256 mismatch: {reference.relative_path}")
    return data


@dataclass(frozen=True)
class SourceDigest:
    relative_path: str
    sha256: str
    byte_length: int


def _read_canonical_source(paths: V5PathBundle, relative_path: str | Path) -> bytes:
    data = _read_root_local_bytes(
        paths,
        relative_path,
        label="source",
        error_type=SourceIntegrityError,
    )
    if b"\r" in data or not data.endswith(b"\n") or data.endswith(b"\n\n"):
        raise SourceIntegrityError(
            f"source is not the single canonical LF representation: {relative_path}"
        )
    return data


def build_source_manifest(
    paths: V5PathBundle,
    relative_paths: Iterable[str | Path],
) -> dict[str, SourceDigest]:
    manifest: dict[str, SourceDigest] = {}
    for relative_path in relative_paths:
        normalized = "/".join(_relative_parts(relative_path))
        if normalized in manifest:
            raise SourceIntegrityError(f"duplicate source path: {normalized}")
        data = _read_canonical_source(paths, normalized)
        manifest[normalized] = SourceDigest(
            relative_path=normalized,
            sha256=hashlib.sha256(data).hexdigest(),
            byte_length=len(data),
        )
    return dict(sorted(manifest.items()))


def verify_source_manifest(
    paths: V5PathBundle,
    manifest: Mapping[str, SourceDigest],
) -> None:
    for relative_path, expected in sorted(manifest.items()):
        normalized = "/".join(_relative_parts(relative_path))
        if normalized != expected.relative_path:
            raise SourceIntegrityError(f"source manifest path mismatch: {relative_path}")
        data = _read_canonical_source(paths, normalized)
        if len(data) != expected.byte_length:
            raise SourceIntegrityError(f"source byte length mismatch: {normalized}")
        if hashlib.sha256(data).hexdigest() != expected.sha256:
            raise SourceIntegrityError(f"source SHA-256 mismatch: {normalized}")
