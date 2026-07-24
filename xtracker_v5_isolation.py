#!/usr/bin/env python3
"""Root-isolated path and exact-byte primitives for future X v5 evidence."""
from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


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

    def __post_init__(self) -> None:
        _validate_source_digest(self)


def _canonical_source_path(raw_path: object) -> str:
    if not isinstance(raw_path, str):
        raise SourceIntegrityError("canonical source path must be a string")
    if not raw_path or "\\" in raw_path or "\x00" in raw_path:
        raise SourceIntegrityError(
            "canonical source path must be a non-empty root-relative POSIX string"
        )
    if raw_path.startswith("/"):
        raise SourceIntegrityError("canonical source path must be root-relative")
    parts = raw_path.split("/")
    if (
        any(part in {"", ".", ".."} for part in parts)
        or parts[0].endswith(":")
        or (
            len(raw_path) >= 2
            and raw_path[0].isalpha()
            and raw_path[1] == ":"
        )
        or "/".join(parts) != raw_path
    ):
        raise SourceIntegrityError(
            f"source path is not canonical root-relative POSIX: {raw_path}"
        )
    return raw_path


def _validate_source_digest(entry: SourceDigest) -> None:
    _canonical_source_path(entry.relative_path)
    if not isinstance(entry.sha256, str) or not _SHA256_PATTERN.fullmatch(
        entry.sha256
    ):
        raise SourceIntegrityError(
            f"source digest SHA-256 must be lowercase hexadecimal: "
            f"{entry.relative_path}"
        )
    if type(entry.byte_length) is not int or entry.byte_length < 0:
        raise SourceIntegrityError(
            f"source digest byte length must be a nonnegative integer: "
            f"{entry.relative_path}"
        )


def _validated_source_paths(
    relative_paths: Iterable[str],
    *,
    role: str,
) -> tuple[str, ...]:
    if isinstance(relative_paths, (str, bytes)):
        raise SourceIntegrityError(f"{role} source paths must be an iterable")
    try:
        raw_paths = tuple(relative_paths)
    except TypeError as exc:
        raise SourceIntegrityError(f"{role} source paths must be an iterable") from exc
    if not raw_paths:
        raise SourceIntegrityError(f"empty {role} source membership is not allowed")

    validated: list[str] = []
    seen: set[str] = set()
    for raw_path in raw_paths:
        canonical = _canonical_source_path(raw_path)
        if canonical in seen:
            raise SourceIntegrityError(
                f"duplicate {role} source path: {canonical}"
            )
        seen.add(canonical)
        validated.append(canonical)
    return tuple(validated)


def _validated_manifest_entries(
    manifest_entries: Iterable[SourceDigest],
) -> tuple[SourceDigest, ...]:
    if isinstance(manifest_entries, (str, bytes)):
        raise SourceIntegrityError("manifest must contain SourceDigest entries")
    try:
        entries = tuple(manifest_entries)
    except TypeError as exc:
        raise SourceIntegrityError(
            "manifest must contain SourceDigest entries"
        ) from exc
    if not entries:
        raise SourceIntegrityError("empty manifest is not allowed")

    validated: list[SourceDigest] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, SourceDigest):
            raise SourceIntegrityError("manifest must contain SourceDigest entries")
        _validate_source_digest(entry)
        if entry.relative_path in seen:
            raise SourceIntegrityError(
                f"duplicate manifest source path: {entry.relative_path}"
            )
        seen.add(entry.relative_path)
        validated.append(entry)
    return tuple(validated)


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
    relative_paths: Iterable[str],
) -> tuple[SourceDigest, ...]:
    requested_paths = _validated_source_paths(relative_paths, role="requested")
    manifest: list[SourceDigest] = []
    for relative_path in sorted(requested_paths):
        data = _read_canonical_source(paths, relative_path)
        manifest.append(
            SourceDigest(
                relative_path=relative_path,
                sha256=hashlib.sha256(data).hexdigest(),
                byte_length=len(data),
            )
        )
    return tuple(manifest)


def verify_source_manifest(
    paths: V5PathBundle,
    manifest_entries: Iterable[SourceDigest],
    expected_relative_paths: Iterable[str],
) -> None:
    expected_paths = _validated_source_paths(
        expected_relative_paths,
        role="expected",
    )
    entries = _validated_manifest_entries(manifest_entries)
    expected_set = set(expected_paths)
    manifest_set = {entry.relative_path for entry in entries}
    if manifest_set != expected_set:
        missing = sorted(expected_set - manifest_set)
        unexpected = sorted(manifest_set - expected_set)
        details: list[str] = []
        if missing:
            details.append(f"missing={missing}")
        if unexpected:
            details.append(f"unexpected={unexpected}")
        raise SourceIntegrityError(
            "source manifest membership mismatch: " + ", ".join(details)
        )

    entries_by_path = {entry.relative_path: entry for entry in entries}
    for relative_path in sorted(expected_set):
        expected = entries_by_path[relative_path]
        data = _read_canonical_source(paths, relative_path)
        if len(data) != expected.byte_length:
            raise SourceIntegrityError(f"source byte length mismatch: {relative_path}")
        if hashlib.sha256(data).hexdigest() != expected.sha256:
            raise SourceIntegrityError(f"source SHA-256 mismatch: {relative_path}")
