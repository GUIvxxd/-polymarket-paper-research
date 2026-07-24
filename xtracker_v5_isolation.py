#!/usr/bin/env python3
"""Root-isolated path and exact-byte primitives for future X v5 evidence."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class RootIsolationError(ValueError):
    pass


class ArtifactIntegrityError(ValueError):
    pass


class SourceIntegrityError(ValueError):
    pass


def _explicit_root(root: Path) -> Path:
    if not isinstance(root, Path):
        raise RootIsolationError("root must be an explicit Path")
    if not root.is_absolute():
        raise RootIsolationError("root must be absolute")
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise RootIsolationError("root must be an existing directory") from exc
    if not resolved.is_dir():
        raise RootIsolationError("root must be an existing directory")
    if root.is_symlink() or resolved != root.absolute():
        raise RootIsolationError("root must not resolve through a symlink")
    return resolved


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
        explicit_root = _explicit_root(root)
        output = explicit_root / "reports" / "xtracker_forward_validation" / "v5"
        return cls(
            root=explicit_root,
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
    path = paths.resolve_relative(reference.relative_path)
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise ArtifactIntegrityError(
            f"artifact is missing under the explicit root: {reference.relative_path}"
        ) from exc
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
    path = paths.resolve_relative(relative_path)
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise SourceIntegrityError(f"source is missing: {relative_path}") from exc
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
