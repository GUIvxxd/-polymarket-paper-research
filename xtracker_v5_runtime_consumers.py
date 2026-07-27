#!/usr/bin/env python3
"""Source-only, paper-only V5 integration for protected artifact consumers.

This module does not deploy, activate, or authorize V5. It provides no network,
wallet, authentication, order, signing, transaction, scheduler, evidence-write,
or runtime-state-write capability. Every artifact read is delegated to the
accepted gateway under a caller-supplied :class:`V5PathBundle`.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Final, Mapping

from xtracker_v5_isolation import SourceDigest, V5PathBundle
from xtracker_v5_runtime_artifacts import (
    ArtifactEnvelope,
    V5RuntimeArtifactGateway,
    VerifiedJSONArtifact,
    validate_artifact_envelope,
)


class RuntimeConsumerContractError(ValueError):
    """A requested artifact batch violates the frozen V5 consumer contract."""


class ArtifactOperationProfile(str, Enum):
    """The complete approved artifact-operation profiles."""

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


@dataclass(frozen=True, slots=True)
class OperationProfileContract:
    """Frozen phase, complete role membership, and market-binding requirement."""

    phase: str
    roles: frozenset[str]
    market_identity_required: bool
    required_provider_reason_code: str | None = None


OPERATION_PROFILE_CONTRACTS: Final[
    Mapping[ArtifactOperationProfile, OperationProfileContract]
] = MappingProxyType(
    {
        ArtifactOperationProfile.CAPTURE_IDENTITY: OperationProfileContract(
            "capture",
            frozenset({"market_identity"}),
            True,
        ),
        ArtifactOperationProfile.CAPTURE_FORECAST: OperationProfileContract(
            "capture",
            frozenset({"forecast_input", "market_identity"}),
            True,
        ),
        ArtifactOperationProfile.CAPTURE_DECISION_CONTEXT: OperationProfileContract(
            "capture",
            frozenset(
                {
                    "forecast_input",
                    "market_identity",
                    "decision_book",
                    "fee_metadata",
                }
            ),
            True,
        ),
        ArtifactOperationProfile.CAPTURE_EXECUTION_ATTEMPT: OperationProfileContract(
            "capture",
            frozenset(
                {
                    "forecast_input",
                    "market_identity",
                    "decision_book",
                    "post_latency_book",
                    "fee_metadata",
                }
            ),
            True,
        ),
        ArtifactOperationProfile.CAPTURE_PROVIDER_OUTCOME_UNIDENTIFIED:
        OperationProfileContract(
            "capture",
            frozenset({"provider_outcome"}),
            False,
        ),
        ArtifactOperationProfile.CAPTURE_PROVIDER_OUTCOME_IDENTIFIED:
        OperationProfileContract(
            "capture",
            frozenset({"provider_outcome", "market_identity"}),
            True,
        ),
        ArtifactOperationProfile.MONITOR_DUE_OPERATION: OperationProfileContract(
            "monitor",
            frozenset({"position_snapshot", "provider_outcome"}),
            True,
        ),
        ArtifactOperationProfile.SETTLEMENT_DUE_OPERATION: OperationProfileContract(
            "settlement",
            frozenset({"position_snapshot", "provider_outcome"}),
            True,
        ),
        ArtifactOperationProfile.SETTLEMENT_FINAL_CORROBORATED:
        OperationProfileContract(
            "settlement",
            frozenset(
                {
                    "finalized_block",
                    "resolution_transaction",
                    "resolution_receipt",
                    "condition_resolution_log",
                    "payout_state",
                    "source_count",
                    "position_snapshot",
                }
            ),
            True,
        ),
        ArtifactOperationProfile.SETTLEMENT_FINAL_XTRACKER_UNAVAILABLE:
        OperationProfileContract(
            "settlement",
            frozenset(
                {
                    "finalized_block",
                    "resolution_transaction",
                    "resolution_receipt",
                    "condition_resolution_log",
                    "payout_state",
                    "position_snapshot",
                    "provider_outcome",
                }
            ),
            True,
            "unavailable",
        ),
    }
)


_PhaseReader = Callable[
    [ArtifactEnvelope, Iterable[SourceDigest], Iterable[str]],
    VerifiedJSONArtifact,
]


def _materialize_envelopes(
    envelopes: Iterable[ArtifactEnvelope],
) -> tuple[ArtifactEnvelope, ...]:
    if isinstance(envelopes, (str, bytes)):
        raise RuntimeConsumerContractError(
            "artifact envelopes must be an iterable of ArtifactEnvelope objects"
        )
    try:
        materialized = tuple(envelopes)
    except TypeError as exc:
        raise RuntimeConsumerContractError(
            "artifact envelopes must be an iterable of ArtifactEnvelope objects"
        ) from exc
    for envelope in materialized:
        if not isinstance(envelope, ArtifactEnvelope):
            raise RuntimeConsumerContractError(
                "artifact inputs must remain ArtifactEnvelope objects"
            )
        if type(envelope.artifact_role) is not str or not envelope.artifact_role:
            raise RuntimeConsumerContractError(
                "artifact envelope roles must be explicit non-empty strings"
            )
    return materialized


def _profile_contract(
    profile: object,
    *,
    phase: str,
) -> OperationProfileContract:
    if not isinstance(profile, ArtifactOperationProfile):
        raise RuntimeConsumerContractError(
            "operation profile must be an ArtifactOperationProfile"
        )
    contract = OPERATION_PROFILE_CONTRACTS.get(profile)
    if contract is None:
        raise RuntimeConsumerContractError(f"unknown operation profile: {profile!r}")
    if contract.phase != phase:
        raise RuntimeConsumerContractError(
            f"operation profile {profile.value} cannot be used for {phase}"
        )
    return contract


def _validate_profile_membership(
    envelopes: tuple[ArtifactEnvelope, ...],
    contract: OperationProfileContract,
) -> None:
    requested: set[str] = set()
    for envelope in envelopes:
        if envelope.artifact_role in requested:
            raise RuntimeConsumerContractError(
                f"duplicate requested role: {envelope.artifact_role!r}"
            )
        requested.add(envelope.artifact_role)

    if requested != contract.roles:
        missing = sorted(contract.roles - requested)
        unexpected = sorted(requested - contract.roles)
        details: list[str] = []
        if missing:
            details.append(f"missing={missing}")
        if unexpected:
            details.append(f"unexpected={unexpected}")
        raise RuntimeConsumerContractError(
            "profile role membership mismatch: " + ", ".join(details)
        )


def _validate_shared_envelope_identity(
    envelopes: tuple[ArtifactEnvelope, ...],
    contract: OperationProfileContract,
) -> None:
    first = envelopes[0]
    shared_fields = (
        "protocol_id",
        "protocol_sha256",
        "complete_lock_sha256",
        "source_manifest_sha256",
        "enrollment",
    )
    for envelope in envelopes[1:]:
        for field_name in shared_fields:
            if getattr(envelope, field_name) != getattr(first, field_name):
                raise RuntimeConsumerContractError(
                    f"mixed batch {field_name} is not allowed"
                )

    if contract.market_identity_required:
        if any(envelope.market_identity is None for envelope in envelopes):
            raise RuntimeConsumerContractError(
                "operation profile requires exact market identity on every artifact"
            )
        if any(
            envelope.market_identity != first.market_identity
            for envelope in envelopes[1:]
        ):
            raise RuntimeConsumerContractError(
                "mixed batch market_identity is not allowed"
            )
    elif any(envelope.market_identity is not None for envelope in envelopes):
        raise RuntimeConsumerContractError(
            "unidentified provider outcome cannot claim market identity"
        )

    if contract.required_provider_reason_code is not None:
        provider_outcome = next(
            envelope
            for envelope in envelopes
            if envelope.artifact_role == "provider_outcome"
        )
        if provider_outcome.reason_code != contract.required_provider_reason_code:
            raise RuntimeConsumerContractError(
                "operation profile requires provider_outcome reason_code="
                f"{contract.required_provider_reason_code!r}"
            )


def _materialize_source_manifest(
    source_manifest: Iterable[SourceDigest],
) -> tuple[SourceDigest, ...]:
    if isinstance(source_manifest, (str, bytes)):
        raise RuntimeConsumerContractError(
            "source manifest must be an explicit iterable of SourceDigest entries"
        )
    try:
        return tuple(source_manifest)
    except TypeError as exc:
        raise RuntimeConsumerContractError(
            "source manifest must be an explicit iterable of SourceDigest entries"
        ) from exc


def _materialize_expected_source_paths(
    expected_source_paths: Iterable[str],
) -> tuple[str, ...]:
    if isinstance(expected_source_paths, (str, bytes)):
        raise RuntimeConsumerContractError(
            "expected source paths must be an explicit iterable of strings"
        )
    try:
        return tuple(expected_source_paths)
    except TypeError as exc:
        raise RuntimeConsumerContractError(
            "expected source paths must be an explicit iterable of strings"
        ) from exc


@dataclass(frozen=True, slots=True)
class V5RuntimeArtifactConsumer:
    """Immutable consumer for the ten frozen V5 artifact-operation profiles."""

    paths: V5PathBundle
    _gateway: V5RuntimeArtifactGateway = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.paths, V5PathBundle):
            raise RuntimeConsumerContractError(
                "consumer construction requires an explicit V5PathBundle"
            )
        object.__setattr__(self, "_gateway", V5RuntimeArtifactGateway(self.paths))

    def consume_capture(
        self,
        profile: ArtifactOperationProfile,
        envelopes: Iterable[ArtifactEnvelope],
        source_manifest: Iterable[SourceDigest],
        expected_source_paths: Iterable[str],
    ) -> tuple[VerifiedJSONArtifact, ...]:
        return self._consume_batch(
            "capture",
            profile,
            envelopes,
            source_manifest,
            expected_source_paths,
            reader=self._gateway.read_capture_json,
        )

    def consume_monitor(
        self,
        profile: ArtifactOperationProfile,
        envelopes: Iterable[ArtifactEnvelope],
        source_manifest: Iterable[SourceDigest],
        expected_source_paths: Iterable[str],
    ) -> tuple[VerifiedJSONArtifact, ...]:
        return self._consume_batch(
            "monitor",
            profile,
            envelopes,
            source_manifest,
            expected_source_paths,
            reader=self._gateway.read_monitor_json,
        )

    def consume_settlement(
        self,
        profile: ArtifactOperationProfile,
        envelopes: Iterable[ArtifactEnvelope],
        source_manifest: Iterable[SourceDigest],
        expected_source_paths: Iterable[str],
    ) -> tuple[VerifiedJSONArtifact, ...]:
        return self._consume_batch(
            "settlement",
            profile,
            envelopes,
            source_manifest,
            expected_source_paths,
            reader=self._gateway.read_settlement_json,
        )

    @staticmethod
    def _consume_batch(
        phase: str,
        profile: ArtifactOperationProfile,
        envelopes: Iterable[ArtifactEnvelope],
        source_manifest: Iterable[SourceDigest],
        expected_source_paths: Iterable[str],
        *,
        reader: _PhaseReader,
    ) -> tuple[VerifiedJSONArtifact, ...]:
        contract = _profile_contract(profile, phase=phase)
        checked_envelopes = _materialize_envelopes(envelopes)
        _validate_profile_membership(checked_envelopes, contract)
        for envelope in checked_envelopes:
            validate_artifact_envelope(phase, envelope)
        _validate_shared_envelope_identity(checked_envelopes, contract)

        preserved_manifest = _materialize_source_manifest(source_manifest)
        preserved_expected_paths = _materialize_expected_source_paths(
            expected_source_paths
        )

        accepted: list[VerifiedJSONArtifact] = []
        for envelope in checked_envelopes:
            accepted.append(
                reader(
                    envelope,
                    preserved_manifest,
                    preserved_expected_paths,
                )
            )
        return tuple(accepted)


__all__ = [
    "ArtifactOperationProfile",
    "OPERATION_PROFILE_CONTRACTS",
    "OperationProfileContract",
    "RuntimeConsumerContractError",
    "V5RuntimeArtifactConsumer",
]
