#!/usr/bin/env python3
"""Source-verified, paper-only JSON artifact gateway for future X v5 consumers.

This module is an integrity boundary only.  It provides no network, wallet,
authentication, order, transaction-submission, scheduler, or write capability.
Every read is rooted in a caller-supplied :class:`V5PathBundle`.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Iterable, Mapping

from xtracker_v5_isolation import (
    ArtifactIntegrityError,
    ArtifactReference,
    SourceDigest,
    V5PathBundle,
    read_verified_artifact,
    verify_source_manifest,
)


CAPTURE_PATH_PREFIX: Final = "reports/xtracker_forward_validation/v5/raw/capture/"
MONITOR_PATH_PREFIX: Final = "reports/xtracker_forward_validation/v5/raw/monitor/"
SETTLEMENT_PATH_PREFIX: Final = (
    "reports/xtracker_forward_validation/v5/settlement_proofs/"
)

CAPTURE_ROLES: Final = frozenset(
    {
        "forecast_input",
        "decision_book",
        "post_latency_book",
        "fee_metadata",
        "market_identity",
    }
)
MONITOR_ROLES: Final = frozenset({"monitor_book", "position_snapshot"})
SETTLEMENT_ROLES: Final = frozenset(
    {
        "finalized_block",
        "resolution_transaction",
        "resolution_receipt",
        "condition_resolution_log",
        "payout_state",
        "source_count",
    }
)

# These labels describe read-only public provenance.  They do not implement or
# authorize fetching.  Settlement evidence is deliberately limited to public
# chain RPC provenance; capture and monitor evidence may originate only from
# the named public read APIs/records.
CAPTURE_SOURCE_TYPES: Final = frozenset(
    {"public_clob_rest", "public_gamma_rest", "public_x_record"}
)
MONITOR_SOURCE_TYPES: Final = frozenset(
    {"public_clob_rest", "public_gamma_rest"}
)
SETTLEMENT_SOURCE_TYPES: Final = frozenset({"public_polygon_rpc"})

_CANONICAL_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class RuntimeArtifactContractError(ValueError):
    """The requested phase, role, path, or provenance violates the gateway API."""


class RuntimeArtifactIntegrityError(ArtifactIntegrityError):
    """Verified artifact bytes do not contain the required strict JSON object."""


@dataclass(frozen=True, slots=True)
class _PhasePolicy:
    prefix: str
    roles: frozenset[str]
    source_types: frozenset[str]


_PHASE_POLICIES: Final[Mapping[str, _PhasePolicy]] = MappingProxyType(
    {
        "capture": _PhasePolicy(
            CAPTURE_PATH_PREFIX,
            CAPTURE_ROLES,
            CAPTURE_SOURCE_TYPES,
        ),
        "monitor": _PhasePolicy(
            MONITOR_PATH_PREFIX,
            MONITOR_ROLES,
            MONITOR_SOURCE_TYPES,
        ),
        "settlement": _PhasePolicy(
            SETTLEMENT_PATH_PREFIX,
            SETTLEMENT_ROLES,
            SETTLEMENT_SOURCE_TYPES,
        ),
    }
)


@dataclass(frozen=True, slots=True)
class VerifiedJSONArtifact:
    """Immutable result retaining identity, exact bytes, digest, and JSON object."""

    phase: str
    reference: ArtifactReference
    raw_bytes: bytes
    sha256: str
    parsed_object: Mapping[str, object]


def _validate_canonical_phase_path(
    relative_path: object,
    *,
    phase: str,
    prefix: str,
) -> str:
    if type(relative_path) is not str:
        raise RuntimeArtifactContractError("artifact path must be an explicit string")
    if not relative_path or "\\" in relative_path or "\x00" in relative_path:
        raise RuntimeArtifactContractError(
            "artifact path must be a non-empty canonical root-relative POSIX string"
        )
    if relative_path.startswith("/"):
        raise RuntimeArtifactContractError("absolute artifact paths are not allowed")

    parts = relative_path.split("/")
    if (
        any(part in {"", ".", ".."} for part in parts)
        or "/".join(parts) != relative_path
        or parts[0].endswith(":")
        or any(_CANONICAL_SEGMENT.fullmatch(part) is None for part in parts)
    ):
        raise RuntimeArtifactContractError(
            "artifact path is not canonical root-relative POSIX"
        )
    if not relative_path.startswith(prefix):
        raise RuntimeArtifactContractError(
            f"artifact path is outside the canonical {phase} prefix"
        )

    phase_local_path = relative_path[len(prefix) :]
    if not phase_local_path or not phase_local_path.endswith(".json"):
        raise RuntimeArtifactContractError(
            f"{phase} artifact path must name a JSON file below its phase prefix"
        )
    return relative_path


def _validate_reference(phase: str, reference: object) -> ArtifactReference:
    if not isinstance(reference, ArtifactReference):
        raise RuntimeArtifactContractError(
            "runtime artifact reference must be an ArtifactReference"
        )
    policy = _PHASE_POLICIES[phase]
    if type(reference.role) is not str or reference.role not in policy.roles:
        raise RuntimeArtifactContractError(
            f"artifact role is not allowed for {phase}: {reference.role!r}"
        )
    if type(reference.source_type) is not str or not reference.source_type:
        raise RuntimeArtifactContractError("artifact source_type must be explicit")
    if reference.source_type not in policy.source_types:
        raise RuntimeArtifactContractError(
            f"artifact source_type is not allowed for {phase}: "
            f"{reference.source_type!r}"
        )
    _validate_canonical_phase_path(
        reference.relative_path,
        phase=phase,
        prefix=policy.prefix,
    )
    return reference


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeArtifactIntegrityError(
                f"JSON object contains duplicate key: {key!r}"
            )
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> object:
    raise RuntimeArtifactIntegrityError(
        f"JSON contains a non-finite numeric constant: {value}"
    )


def _strict_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise RuntimeArtifactIntegrityError("JSON number is outside the finite range")
    return parsed


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _decode_strict_json_object(raw_bytes: bytes) -> Mapping[str, object]:
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        raise RuntimeArtifactIntegrityError("UTF-8 BOM is not allowed")
    try:
        text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RuntimeArtifactIntegrityError("artifact is not strict UTF-8") from exc

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonfinite_constant,
            parse_float=_strict_float,
        )
    except RuntimeArtifactIntegrityError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise RuntimeArtifactIntegrityError("artifact contains malformed JSON") from exc

    if not isinstance(parsed, dict):
        raise RuntimeArtifactIntegrityError("JSON artifact top level must be an object")
    frozen = _freeze_json(parsed)
    if not isinstance(frozen, Mapping):
        raise RuntimeArtifactIntegrityError("JSON artifact object could not be frozen")
    return frozen


@dataclass(frozen=True, slots=True)
class V5RuntimeArtifactGateway:
    """Explicit-root gateway used separately by capture, monitor, and settlement."""

    paths: V5PathBundle

    def __post_init__(self) -> None:
        if not isinstance(self.paths, V5PathBundle):
            raise RuntimeArtifactContractError(
                "gateway construction requires an explicit V5PathBundle"
            )

    def read_capture_json(
        self,
        reference: ArtifactReference,
        source_manifest: Iterable[SourceDigest],
        expected_source_paths: Iterable[str],
    ) -> VerifiedJSONArtifact:
        return self._read_phase_json(
            "capture",
            reference,
            source_manifest,
            expected_source_paths,
        )

    def read_monitor_json(
        self,
        reference: ArtifactReference,
        source_manifest: Iterable[SourceDigest],
        expected_source_paths: Iterable[str],
    ) -> VerifiedJSONArtifact:
        return self._read_phase_json(
            "monitor",
            reference,
            source_manifest,
            expected_source_paths,
        )

    def read_settlement_json(
        self,
        reference: ArtifactReference,
        source_manifest: Iterable[SourceDigest],
        expected_source_paths: Iterable[str],
    ) -> VerifiedJSONArtifact:
        return self._read_phase_json(
            "settlement",
            reference,
            source_manifest,
            expected_source_paths,
        )

    def _read_phase_json(
        self,
        phase: str,
        reference: ArtifactReference,
        source_manifest: Iterable[SourceDigest],
        expected_source_paths: Iterable[str],
    ) -> VerifiedJSONArtifact:
        checked_reference = _validate_reference(phase, reference)

        # Membership is supplied independently from the duplicate-preserving
        # manifest.  Never infer the expected set from manifest entries.
        verify_source_manifest(
            self.paths,
            source_manifest,
            expected_source_paths,
        )

        # This accepted primitive is the sole artifact byte-read boundary.
        raw_bytes = read_verified_artifact(self.paths, checked_reference)
        parsed_object = _decode_strict_json_object(raw_bytes)
        return VerifiedJSONArtifact(
            phase=phase,
            reference=checked_reference,
            raw_bytes=raw_bytes,
            sha256=checked_reference.sha256,
            parsed_object=parsed_object,
        )


__all__ = [
    "CAPTURE_PATH_PREFIX",
    "CAPTURE_ROLES",
    "CAPTURE_SOURCE_TYPES",
    "MONITOR_PATH_PREFIX",
    "MONITOR_ROLES",
    "MONITOR_SOURCE_TYPES",
    "RuntimeArtifactContractError",
    "RuntimeArtifactIntegrityError",
    "SETTLEMENT_PATH_PREFIX",
    "SETTLEMENT_ROLES",
    "SETTLEMENT_SOURCE_TYPES",
    "V5RuntimeArtifactGateway",
    "VerifiedJSONArtifact",
]
