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

from xtracker_v5_isolation import ArtifactReference, SourceDigest, V5PathBundle
from xtracker_v5_runtime_artifacts import (
    V5RuntimeArtifactGateway,
    VerifiedJSONArtifact,
)


class RuntimeConsumerContractError(ValueError):
    """A requested artifact batch violates the V5 consumer contract."""


_PhaseReader = Callable[
    [ArtifactReference, Iterable[SourceDigest], Iterable[str]],
    VerifiedJSONArtifact,
]


def _materialize_references(
    references: Iterable[ArtifactReference],
) -> tuple[ArtifactReference, ...]:
    if isinstance(references, (str, bytes)):
        raise RuntimeConsumerContractError(
            "artifact references must be an iterable of ArtifactReference objects"
        )
    try:
        materialized = tuple(references)
    except TypeError as exc:
        raise RuntimeConsumerContractError(
            "artifact references must be an iterable of ArtifactReference objects"
        ) from exc
    for reference in materialized:
        if not isinstance(reference, ArtifactReference):
            raise RuntimeConsumerContractError(
                "artifact references must remain ArtifactReference objects"
            )
        if type(reference.role) is not str or not reference.role:
            raise RuntimeConsumerContractError(
                "artifact reference roles must be explicit non-empty strings"
            )
    return materialized


def _materialize_required_roles(required_roles: Iterable[str]) -> tuple[str, ...]:
    if isinstance(required_roles, (str, bytes)):
        raise RuntimeConsumerContractError(
            "required roles must be an explicit iterable of strings"
        )
    try:
        materialized = tuple(required_roles)
    except TypeError as exc:
        raise RuntimeConsumerContractError(
            "required roles must be an explicit iterable of strings"
        ) from exc
    if not materialized:
        raise RuntimeConsumerContractError("required role membership cannot be empty")

    seen: set[str] = set()
    for role in materialized:
        if type(role) is not str or not role:
            raise RuntimeConsumerContractError(
                "required roles must be explicit non-empty strings"
            )
        if role in seen:
            raise RuntimeConsumerContractError(f"duplicate required role: {role!r}")
        seen.add(role)
    return materialized


def _validate_batch_membership(
    references: Iterable[ArtifactReference],
    required_roles: Iterable[str],
) -> tuple[ArtifactReference, ...]:
    materialized_references = _materialize_references(references)
    materialized_required_roles = _materialize_required_roles(required_roles)

    requested: set[str] = set()
    for reference in materialized_references:
        if reference.role in requested:
            raise RuntimeConsumerContractError(
                f"duplicate requested role: {reference.role!r}"
            )
        requested.add(reference.role)

    required = set(materialized_required_roles)
    if requested != required:
        missing = sorted(required - requested)
        unexpected = sorted(requested - required)
        details: list[str] = []
        if missing:
            details.append(f"missing={missing}")
        if unexpected:
            details.append(f"unexpected={unexpected}")
        raise RuntimeConsumerContractError(
            "requested role membership mismatch: " + ", ".join(details)
        )
    return materialized_references


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
    """Immutable batch consumer for capture, monitor, and settlement artifacts."""

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
        references: Iterable[ArtifactReference],
        required_roles: Iterable[str],
        source_manifest: Iterable[SourceDigest],
        expected_source_paths: Iterable[str],
    ) -> tuple[VerifiedJSONArtifact, ...]:
        return self._consume_batch(
            references,
            required_roles,
            source_manifest,
            expected_source_paths,
            reader=self._gateway.read_capture_json,
        )

    def consume_monitor(
        self,
        references: Iterable[ArtifactReference],
        required_roles: Iterable[str],
        source_manifest: Iterable[SourceDigest],
        expected_source_paths: Iterable[str],
    ) -> tuple[VerifiedJSONArtifact, ...]:
        return self._consume_batch(
            references,
            required_roles,
            source_manifest,
            expected_source_paths,
            reader=self._gateway.read_monitor_json,
        )

    def consume_settlement(
        self,
        references: Iterable[ArtifactReference],
        required_roles: Iterable[str],
        source_manifest: Iterable[SourceDigest],
        expected_source_paths: Iterable[str],
    ) -> tuple[VerifiedJSONArtifact, ...]:
        return self._consume_batch(
            references,
            required_roles,
            source_manifest,
            expected_source_paths,
            reader=self._gateway.read_settlement_json,
        )

    @staticmethod
    def _consume_batch(
        references: Iterable[ArtifactReference],
        required_roles: Iterable[str],
        source_manifest: Iterable[SourceDigest],
        expected_source_paths: Iterable[str],
        *,
        reader: _PhaseReader,
    ) -> tuple[VerifiedJSONArtifact, ...]:
        checked_references = _validate_batch_membership(
            references,
            required_roles,
        )
        preserved_manifest = _materialize_source_manifest(source_manifest)
        preserved_expected_paths = _materialize_expected_source_paths(
            expected_source_paths
        )

        accepted: list[VerifiedJSONArtifact] = []
        for reference in checked_references:
            accepted.append(
                reader(
                    reference,
                    preserved_manifest,
                    preserved_expected_paths,
                )
            )
        return tuple(accepted)


__all__ = [
    "RuntimeConsumerContractError",
    "V5RuntimeArtifactConsumer",
]
