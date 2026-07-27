#!/usr/bin/env python3
"""Source-verified, paper-only JSON artifact gateway for X v5 consumers.

This module is an integrity boundary only. It provides no network, wallet,
authentication, order, transaction-submission, scheduler, or write capability.
Every read is rooted in a caller-supplied :class:`V5PathBundle`.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
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


RAW_EXTERNAL_ARTIFACT: Final = "raw_external_artifact"
DERIVED_LOCAL_RECORD: Final = "derived_local_record"
ARTIFACT_ENVELOPE_SCHEMA_ID: Final = "xtracker_v5_artifact_envelope"
ARTIFACT_ENVELOPE_SCHEMA_VERSION: Final = "1"

CAPTURE_PATH_PREFIX: Final = "reports/xtracker_forward_validation/v5/raw/capture/"
CAPTURE_DERIVED_PATH_PREFIX: Final = (
    "reports/xtracker_forward_validation/v5/derived/capture/"
)
MONITOR_DERIVED_PATH_PREFIX: Final = (
    "reports/xtracker_forward_validation/v5/derived/monitor/"
)
SETTLEMENT_PATH_PREFIX: Final = (
    "reports/xtracker_forward_validation/v5/settlement_proofs/"
)
SETTLEMENT_DERIVED_PATH_PREFIX: Final = (
    "reports/xtracker_forward_validation/v5/derived/settlement/"
)

CAPTURE_ROLES: Final = frozenset(
    {
        "forecast_input",
        "decision_book",
        "post_latency_book",
        "fee_metadata",
        "market_identity",
        "provider_outcome",
    }
)
MONITOR_ROLES: Final = frozenset({"position_snapshot", "provider_outcome"})
SETTLEMENT_ROLES: Final = frozenset(
    {
        "finalized_block",
        "resolution_transaction",
        "resolution_receipt",
        "condition_resolution_log",
        "payout_state",
        "source_count",
        "position_snapshot",
        "provider_outcome",
    }
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_UTC_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)


class ArtifactOperationProfile(str, Enum):
    """The complete approved artifact-operation profile identities."""

    CAPTURE_IDENTITY = "CAPTURE_IDENTITY"
    CAPTURE_FORECAST = "CAPTURE_FORECAST"
    CAPTURE_DECISION_CONTEXT = "CAPTURE_DECISION_CONTEXT"
    CAPTURE_EXECUTION_ATTEMPT = "CAPTURE_EXECUTION_ATTEMPT"
    CAPTURE_PROVIDER_OUTCOME_UNIDENTIFIED = (
        "CAPTURE_PROVIDER_OUTCOME_UNIDENTIFIED"
    )
    CAPTURE_PROVIDER_OUTCOME_IDENTIFIED = "CAPTURE_PROVIDER_OUTCOME_IDENTIFIED"
    MONITOR_DUE_OPERATION = "MONITOR_DUE_OPERATION"
    SETTLEMENT_DUE_OPERATION = "SETTLEMENT_DUE_OPERATION"
    SETTLEMENT_FINAL_CORROBORATED = "SETTLEMENT_FINAL_CORROBORATED"
    SETTLEMENT_FINAL_XTRACKER_UNAVAILABLE = (
        "SETTLEMENT_FINAL_XTRACKER_UNAVAILABLE"
    )


class RuntimeArtifactContractError(ValueError):
    """The requested phase, role, envelope, or provenance violates the API."""


class RuntimeArtifactIntegrityError(ArtifactIntegrityError):
    """Verified artifact bytes do not contain the required strict JSON object."""


@dataclass(frozen=True, slots=True)
class RoleProvenance:
    """Frozen evidence class and sole accepted source for one logical role."""

    evidence_class: str
    source_type: str


ROLE_PROVENANCE: Final[Mapping[str, RoleProvenance]] = MappingProxyType(
    {
        "forecast_input": RoleProvenance(
            RAW_EXTERNAL_ARTIFACT,
            "public_x_record",
        ),
        "market_identity": RoleProvenance(
            RAW_EXTERNAL_ARTIFACT,
            "public_gamma_rest",
        ),
        "decision_book": RoleProvenance(
            RAW_EXTERNAL_ARTIFACT,
            "public_clob_rest",
        ),
        "post_latency_book": RoleProvenance(
            RAW_EXTERNAL_ARTIFACT,
            "public_clob_rest",
        ),
        "fee_metadata": RoleProvenance(
            RAW_EXTERNAL_ARTIFACT,
            "public_clob_rest",
        ),
        "position_snapshot": RoleProvenance(
            DERIVED_LOCAL_RECORD,
            "derived_v5_lifecycle",
        ),
        "provider_outcome": RoleProvenance(
            DERIVED_LOCAL_RECORD,
            "derived_v5_provider_outcome",
        ),
        "finalized_block": RoleProvenance(
            RAW_EXTERNAL_ARTIFACT,
            "public_polygon_rpc",
        ),
        "resolution_transaction": RoleProvenance(
            RAW_EXTERNAL_ARTIFACT,
            "public_polygon_rpc",
        ),
        "resolution_receipt": RoleProvenance(
            RAW_EXTERNAL_ARTIFACT,
            "public_polygon_rpc",
        ),
        "condition_resolution_log": RoleProvenance(
            RAW_EXTERNAL_ARTIFACT,
            "public_polygon_rpc",
        ),
        "payout_state": RoleProvenance(
            RAW_EXTERNAL_ARTIFACT,
            "public_polygon_rpc",
        ),
        "source_count": RoleProvenance(
            RAW_EXTERNAL_ARTIFACT,
            "public_x_record",
        ),
    }
)


_PHASE_ROLES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "capture": CAPTURE_ROLES,
        "monitor": MONITOR_ROLES,
        "settlement": SETTLEMENT_ROLES,
    }
)


_PROVIDER_OUTCOME_PROFILE_PHASES: Final[
    Mapping[ArtifactOperationProfile, str]
] = MappingProxyType(
    {
        ArtifactOperationProfile.CAPTURE_PROVIDER_OUTCOME_UNIDENTIFIED: "capture",
        ArtifactOperationProfile.CAPTURE_PROVIDER_OUTCOME_IDENTIFIED: "capture",
        ArtifactOperationProfile.MONITOR_DUE_OPERATION: "monitor",
        ArtifactOperationProfile.SETTLEMENT_DUE_OPERATION: "settlement",
        ArtifactOperationProfile.SETTLEMENT_FINAL_XTRACKER_UNAVAILABLE: "settlement",
    }
)


def artifact_path_prefix(phase: str, role: str) -> str:
    """Return the frozen root-relative prefix for a valid phase/role pair."""

    if phase not in _PHASE_ROLES or role not in _PHASE_ROLES[phase]:
        raise RuntimeArtifactContractError(
            f"artifact role is not allowed for {phase}: {role!r}"
        )
    if phase == "capture":
        if role == "provider_outcome":
            return CAPTURE_DERIVED_PATH_PREFIX
        return CAPTURE_PATH_PREFIX
    if phase == "monitor":
        return MONITOR_DERIVED_PATH_PREFIX
    if role in {"position_snapshot", "provider_outcome"}:
        return SETTLEMENT_DERIVED_PATH_PREFIX
    return SETTLEMENT_PATH_PREFIX


@dataclass(frozen=True, slots=True)
class EnrollmentWindowIdentity:
    """Immutable enrollment and forecast-window identity."""

    platform: str
    normalized_handle: str
    xtracker_tracking_id: str
    window_start_utc: str
    window_end_utc: str
    gamma_event_id: str


@dataclass(frozen=True, slots=True)
class MarketOutcomeIdentity:
    """Exact market, condition, token, and outcome identity without normalization."""

    gamma_market_id: str
    condition_id: str
    token_id: str
    outcome: str


@dataclass(frozen=True, slots=True)
class ArtifactEnvelope:
    """Immutable metadata bound one-to-one to an exact ArtifactReference."""

    reference: ArtifactReference
    schema_id: str
    schema_version: str
    artifact_role: str
    evidence_class: str
    protocol_id: str
    protocol_sha256: str
    complete_lock_sha256: str
    source_manifest_sha256: str
    source_type: str
    enrollment: EnrollmentWindowIdentity
    market_identity: MarketOutcomeIdentity | None
    request_started_at_utc: str | None
    response_received_at_utc: str | None
    semantic_observed_at_utc: str | None
    canonical_relative_path: str
    byte_length: int
    content_sha256: str
    operation_profile: ArtifactOperationProfile | None
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class VerifiedJSONArtifact:
    """Immutable result retaining envelope, identity, exact bytes, and JSON."""

    phase: str
    envelope: ArtifactEnvelope
    reference: ArtifactReference
    raw_bytes: bytes
    sha256: str
    parsed_object: Mapping[str, object]


def _validate_canonical_identifier(value: object, *, label: str) -> str:
    if type(value) is not str or _CANONICAL_SEGMENT.fullmatch(value) is None:
        raise RuntimeArtifactContractError(
            f"{label} must be a non-empty canonical string"
        )
    return value


def _validate_exact_identity_text(value: object, *, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
    ):
        raise RuntimeArtifactContractError(
            f"{label} must be an exact non-empty identity string"
        )
    return value


def _validate_sha256(value: object, *, label: str) -> str:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise RuntimeArtifactContractError(
            f"{label} must be 64 lowercase hexadecimal characters"
        )
    return value


def _validate_utc_timestamp(
    value: object,
    *,
    label: str,
    optional: bool,
) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise RuntimeArtifactContractError(
            f"{label} must be a canonical UTC timestamp ending in Z"
        )
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RuntimeArtifactContractError(
            f"{label} must be a valid UTC timestamp"
        ) from exc
    return value


def _validate_enrollment_identity(
    identity: object,
) -> EnrollmentWindowIdentity:
    if not isinstance(identity, EnrollmentWindowIdentity):
        raise RuntimeArtifactContractError(
            "enrollment must be an EnrollmentWindowIdentity"
        )
    _validate_canonical_identifier(identity.platform, label="platform")
    _validate_canonical_identifier(
        identity.normalized_handle,
        label="normalized_handle",
    )
    _validate_canonical_identifier(
        identity.xtracker_tracking_id,
        label="xtracker_tracking_id",
    )
    _validate_utc_timestamp(
        identity.window_start_utc,
        label="window_start_utc",
        optional=False,
    )
    _validate_utc_timestamp(
        identity.window_end_utc,
        label="window_end_utc",
        optional=False,
    )
    _validate_canonical_identifier(
        identity.gamma_event_id,
        label="gamma_event_id",
    )
    return identity


def _validate_market_identity(
    identity: object,
) -> MarketOutcomeIdentity | None:
    if identity is None:
        return None
    if not isinstance(identity, MarketOutcomeIdentity):
        raise RuntimeArtifactContractError(
            "market_identity must be a MarketOutcomeIdentity or None"
        )
    _validate_exact_identity_text(
        identity.gamma_market_id,
        label="gamma_market_id",
    )
    _validate_exact_identity_text(identity.condition_id, label="condition_id")
    _validate_exact_identity_text(identity.token_id, label="token_id")
    _validate_exact_identity_text(identity.outcome, label="outcome")
    return identity


def _validate_canonical_phase_path(
    relative_path: object,
    *,
    phase: str,
    role: str,
    content_sha256: str,
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

    prefix = artifact_path_prefix(phase, role)
    if not relative_path.startswith(prefix):
        raise RuntimeArtifactContractError(
            f"artifact path is outside the canonical {phase}/{role} prefix"
        )
    local_path = relative_path[len(prefix) :]
    if not local_path or parts[-1] != f"{content_sha256}.json":
        raise RuntimeArtifactContractError(
            "artifact path must use the verified content SHA-256 as its filename"
        )
    return relative_path


def validate_artifact_envelope(
    phase: str,
    envelope: object,
) -> ArtifactReference:
    """Validate the complete no-I/O envelope contract for one phase."""

    if phase not in _PHASE_ROLES:
        raise RuntimeArtifactContractError(f"unknown artifact phase: {phase!r}")
    if not isinstance(envelope, ArtifactEnvelope):
        raise RuntimeArtifactContractError(
            "runtime artifact input must be an ArtifactEnvelope"
        )
    if not isinstance(envelope.reference, ArtifactReference):
        raise RuntimeArtifactContractError(
            "artifact envelope must bind an ArtifactReference"
        )
    if envelope.schema_id != ARTIFACT_ENVELOPE_SCHEMA_ID:
        raise RuntimeArtifactContractError("unknown artifact envelope schema_id")
    if envelope.schema_version != ARTIFACT_ENVELOPE_SCHEMA_VERSION:
        raise RuntimeArtifactContractError("unknown artifact envelope schema_version")

    reference = envelope.reference
    if type(envelope.artifact_role) is not str:
        raise RuntimeArtifactContractError("artifact_role must be an explicit string")
    if envelope.artifact_role != reference.role:
        raise RuntimeArtifactContractError(
            "envelope artifact_role does not match its ArtifactReference"
        )
    if envelope.artifact_role not in _PHASE_ROLES[phase]:
        raise RuntimeArtifactContractError(
            f"artifact role is not allowed for {phase}: {envelope.artifact_role!r}"
        )

    provenance = ROLE_PROVENANCE.get(envelope.artifact_role)
    if provenance is None:
        raise RuntimeArtifactContractError(
            f"artifact role has no frozen provenance: {envelope.artifact_role!r}"
        )
    if envelope.evidence_class != provenance.evidence_class:
        raise RuntimeArtifactContractError(
            f"artifact evidence_class is not allowed for "
            f"{envelope.artifact_role}: {envelope.evidence_class!r}"
        )
    if envelope.source_type != provenance.source_type:
        raise RuntimeArtifactContractError(
            f"artifact source_type is not allowed for "
            f"{envelope.artifact_role}: {envelope.source_type!r}"
        )
    if envelope.source_type != reference.source_type:
        raise RuntimeArtifactContractError(
            "envelope source_type does not match its ArtifactReference"
        )

    _validate_canonical_identifier(envelope.protocol_id, label="protocol_id")
    _validate_sha256(envelope.protocol_sha256, label="protocol_sha256")
    _validate_sha256(
        envelope.complete_lock_sha256,
        label="complete_lock_sha256",
    )
    _validate_sha256(
        envelope.source_manifest_sha256,
        label="source_manifest_sha256",
    )
    _validate_enrollment_identity(envelope.enrollment)
    _validate_market_identity(envelope.market_identity)
    _validate_utc_timestamp(
        envelope.request_started_at_utc,
        label="request_started_at_utc",
        optional=True,
    )
    _validate_utc_timestamp(
        envelope.response_received_at_utc,
        label="response_received_at_utc",
        optional=True,
    )
    _validate_utc_timestamp(
        envelope.semantic_observed_at_utc,
        label="semantic_observed_at_utc",
        optional=True,
    )
    if envelope.reason_code is not None:
        _validate_canonical_identifier(envelope.reason_code, label="reason_code")
    if envelope.artifact_role == "provider_outcome":
        if envelope.reason_code is None:
            raise RuntimeArtifactContractError(
                "provider_outcome requires a deterministic reason_code"
            )
        if not isinstance(
            envelope.operation_profile,
            ArtifactOperationProfile,
        ):
            raise RuntimeArtifactContractError(
                "provider_outcome requires an ArtifactOperationProfile binding"
            )
        operation_phase = _PROVIDER_OUTCOME_PROFILE_PHASES.get(
            envelope.operation_profile
        )
        if operation_phase is None:
            raise RuntimeArtifactContractError(
                "provider_outcome operation profile does not accept that role"
            )
        if operation_phase != phase:
            raise RuntimeArtifactContractError(
                "provider_outcome operation profile does not match artifact phase"
            )
    elif envelope.operation_profile is not None:
        raise RuntimeArtifactContractError(
            "operation_profile is reserved for provider_outcome evidence"
        )

    _validate_sha256(envelope.content_sha256, label="content_sha256")
    if type(envelope.byte_length) is not int or envelope.byte_length < 0:
        raise RuntimeArtifactContractError(
            "byte_length must be a nonnegative integer"
        )
    if envelope.canonical_relative_path != reference.relative_path:
        raise RuntimeArtifactContractError(
            "envelope path does not match its ArtifactReference"
        )
    if envelope.byte_length != reference.byte_length:
        raise RuntimeArtifactContractError(
            "envelope byte length does not match its ArtifactReference"
        )
    if envelope.content_sha256 != reference.sha256:
        raise RuntimeArtifactContractError(
            "envelope content SHA-256 does not match its ArtifactReference"
        )
    _validate_canonical_phase_path(
        envelope.canonical_relative_path,
        phase=phase,
        role=envelope.artifact_role,
        content_sha256=envelope.content_sha256,
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
    try:
        frozen = _freeze_json(parsed)
    except RecursionError as exc:
        raise RuntimeArtifactIntegrityError(
            "JSON artifact exceeds immutable conversion depth"
        ) from exc
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
        envelope: ArtifactEnvelope,
        source_manifest: Iterable[SourceDigest],
        expected_source_paths: Iterable[str],
    ) -> VerifiedJSONArtifact:
        return self._read_phase_json(
            "capture",
            envelope,
            source_manifest,
            expected_source_paths,
        )

    def read_monitor_json(
        self,
        envelope: ArtifactEnvelope,
        source_manifest: Iterable[SourceDigest],
        expected_source_paths: Iterable[str],
    ) -> VerifiedJSONArtifact:
        return self._read_phase_json(
            "monitor",
            envelope,
            source_manifest,
            expected_source_paths,
        )

    def read_settlement_json(
        self,
        envelope: ArtifactEnvelope,
        source_manifest: Iterable[SourceDigest],
        expected_source_paths: Iterable[str],
    ) -> VerifiedJSONArtifact:
        return self._read_phase_json(
            "settlement",
            envelope,
            source_manifest,
            expected_source_paths,
        )

    def _read_phase_json(
        self,
        phase: str,
        envelope: ArtifactEnvelope,
        source_manifest: Iterable[SourceDigest],
        expected_source_paths: Iterable[str],
    ) -> VerifiedJSONArtifact:
        checked_reference = validate_artifact_envelope(phase, envelope)

        verify_source_manifest(
            self.paths,
            source_manifest,
            expected_source_paths,
        )

        raw_bytes = read_verified_artifact(self.paths, checked_reference)
        parsed_object = _decode_strict_json_object(raw_bytes)
        return VerifiedJSONArtifact(
            phase=phase,
            envelope=envelope,
            reference=checked_reference,
            raw_bytes=raw_bytes,
            sha256=checked_reference.sha256,
            parsed_object=parsed_object,
        )


__all__ = [
    "ARTIFACT_ENVELOPE_SCHEMA_ID",
    "ARTIFACT_ENVELOPE_SCHEMA_VERSION",
    "ArtifactOperationProfile",
    "ArtifactEnvelope",
    "CAPTURE_DERIVED_PATH_PREFIX",
    "CAPTURE_PATH_PREFIX",
    "CAPTURE_ROLES",
    "DERIVED_LOCAL_RECORD",
    "EnrollmentWindowIdentity",
    "MONITOR_DERIVED_PATH_PREFIX",
    "MONITOR_ROLES",
    "MarketOutcomeIdentity",
    "RAW_EXTERNAL_ARTIFACT",
    "ROLE_PROVENANCE",
    "RoleProvenance",
    "RuntimeArtifactContractError",
    "RuntimeArtifactIntegrityError",
    "SETTLEMENT_DERIVED_PATH_PREFIX",
    "SETTLEMENT_PATH_PREFIX",
    "SETTLEMENT_ROLES",
    "V5RuntimeArtifactGateway",
    "VerifiedJSONArtifact",
    "artifact_path_prefix",
    "validate_artifact_envelope",
]
