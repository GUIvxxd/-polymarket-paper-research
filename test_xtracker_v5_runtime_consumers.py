from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest import mock

import xtracker_v5_isolation as isolation
import xtracker_v5_runtime_artifacts as runtime
import xtracker_v5_runtime_consumers as consumers


SOURCE_ROOT = Path(__file__).resolve().parent
CONSUMER_SOURCE = SOURCE_ROOT / "xtracker_v5_runtime_consumers.py"
_HASH_A = "1" * 64
_HASH_B = "2" * 64
_HASH_C = "3" * 64
_DEFAULT = object()


class V5RuntimeArtifactConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = tempfile.TemporaryDirectory()
        self.sandbox_root = Path(self.sandbox.name).resolve()
        self.root = self.sandbox_root / "explicit-v5-root"
        self.root.mkdir()
        self.outside = (
            self.sandbox_root
            / "canonical-looking-external"
            / "data"
            / "workspace"
            / "polymarket-research"
        )
        self.outside.mkdir(parents=True)
        self.paths = isolation.V5PathBundle.from_root(self.root)
        self.consumer = consumers.V5RuntimeArtifactConsumer(self.paths)

        self.source_path = "src/future_v5_runtime_consumer.py"
        self.write_under_root(self.source_path, b"PAPER_ONLY = True\n")
        self.expected_sources = (self.source_path,)
        self.source_manifest = isolation.build_source_manifest(
            self.paths,
            self.expected_sources,
        )
        self.enrollment = runtime.EnrollmentWindowIdentity(
            platform="x",
            normalized_handle="example_handle",
            xtracker_tracking_id="tracking_123",
            window_start_utc="2026-07-27T00:00:00Z",
            window_end_utc="2026-07-28T00:00:00Z",
            gamma_event_id="gamma_event_123",
        )
        self.market_identity = runtime.MarketOutcomeIdentity(
            gamma_market_id="gamma_market_123",
            condition_id="0xcondition",
            token_id="123456789",
            outcome="Up",
        )

    def tearDown(self) -> None:
        self.sandbox.cleanup()

    def write_under_root(self, relative_path: str, data: bytes) -> Path:
        target = self.root.joinpath(*relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def write_under_outside(self, relative_path: str, data: bytes) -> Path:
        target = self.outside.joinpath(*relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def make_envelope(
        self,
        *,
        phase: str,
        role: str,
        data: bytes,
        source_type: str | None = None,
        evidence_class: str | None = None,
        write: bool = True,
        market_identity: object = _DEFAULT,
        protocol_id: str = "xtracker_forward_v5_test",
        protocol_sha256: str = _HASH_A,
        complete_lock_sha256: str = _HASH_B,
        source_manifest_sha256: str = _HASH_C,
        schema_id: str = runtime.ARTIFACT_ENVELOPE_SCHEMA_ID,
        schema_version: str = runtime.ARTIFACT_ENVELOPE_SCHEMA_VERSION,
        reason_code: object = _DEFAULT,
    ) -> runtime.ArtifactEnvelope:
        provenance = runtime.ROLE_PROVENANCE[role]
        selected_source = source_type or provenance.source_type
        selected_class = evidence_class or provenance.evidence_class
        digest = hashlib.sha256(data).hexdigest()
        relative_path = (
            runtime.artifact_path_prefix(phase, role) + digest + ".json"
        )
        reference = isolation.ArtifactReference(
            role=role,
            relative_path=relative_path,
            sha256=digest,
            byte_length=len(data),
            source_type=selected_source,
        )
        if write:
            self.write_under_root(relative_path, data)
        return runtime.ArtifactEnvelope(
            reference=reference,
            schema_id=schema_id,
            schema_version=schema_version,
            artifact_role=role,
            evidence_class=selected_class,
            protocol_id=protocol_id,
            protocol_sha256=protocol_sha256,
            complete_lock_sha256=complete_lock_sha256,
            source_manifest_sha256=source_manifest_sha256,
            source_type=selected_source,
            enrollment=self.enrollment,
            market_identity=(
                self.market_identity
                if market_identity is _DEFAULT
                else market_identity
            ),
            request_started_at_utc="2026-07-27T12:00:00.100000Z",
            response_received_at_utc="2026-07-27T12:00:00.200000Z",
            semantic_observed_at_utc="2026-07-27T12:00:00Z",
            canonical_relative_path=relative_path,
            byte_length=len(data),
            content_sha256=digest,
            reason_code=(
                "available"
                if reason_code is _DEFAULT and role == "provider_outcome"
                else None if reason_code is _DEFAULT else reason_code
            ),  # type: ignore[arg-type]
        )

    def profile_envelopes(
        self,
        profile: consumers.ArtifactOperationProfile,
    ) -> tuple[runtime.ArtifactEnvelope, ...]:
        contract = consumers.OPERATION_PROFILE_CONTRACTS[profile]
        market_identity = (
            self.market_identity if contract.market_identity_required else None
        )
        return tuple(
            self.make_envelope(
                phase=contract.phase,
                role=role,
                data=(
                    f'{{"profile":"{profile.value}","role":"{role}"}}\n'
                ).encode(),
                market_identity=market_identity,
                reason_code=(
                    "unavailable"
                    if (
                        profile
                        is consumers.ArtifactOperationProfile.SETTLEMENT_FINAL_XTRACKER_UNAVAILABLE
                        and role == "provider_outcome"
                    )
                    else _DEFAULT
                ),
            )
            for role in sorted(contract.roles)
        )

    def consume(
        self,
        profile: consumers.ArtifactOperationProfile,
        envelopes: tuple[runtime.ArtifactEnvelope, ...],
        *,
        source_manifest: tuple[isolation.SourceDigest, ...] | None = None,
        expected_sources: tuple[str, ...] | None = None,
    ) -> tuple[runtime.VerifiedJSONArtifact, ...]:
        phase = consumers.OPERATION_PROFILE_CONTRACTS[profile].phase
        methods = {
            "capture": self.consumer.consume_capture,
            "monitor": self.consumer.consume_monitor,
            "settlement": self.consumer.consume_settlement,
        }
        return methods[phase](
            profile,
            envelopes,
            self.source_manifest if source_manifest is None else source_manifest,
            self.expected_sources if expected_sources is None else expected_sources,
        )

    def test_every_approved_profile_accepts_exactly_its_frozen_role_set(self) -> None:
        self.assertEqual(len(consumers.OPERATION_PROFILE_CONTRACTS), 10)
        for profile, contract in consumers.OPERATION_PROFILE_CONTRACTS.items():
            with self.subTest(profile=profile.value):
                envelopes = self.profile_envelopes(profile)
                result = self.consume(profile, envelopes)
                self.assertEqual(
                    {artifact.reference.role for artifact in result},
                    contract.roles,
                )
                self.assertTrue(
                    all(artifact.phase == contract.phase for artifact in result)
                )
                self.assertTrue(
                    all(
                        artifact.envelope is envelope
                        for artifact, envelope in zip(result, envelopes)
                    )
                )

    def test_profile_contracts_match_the_approved_exact_roles(self) -> None:
        expected = {
            consumers.ArtifactOperationProfile.CAPTURE_IDENTITY: {
                "market_identity"
            },
            consumers.ArtifactOperationProfile.CAPTURE_FORECAST: {
                "forecast_input",
                "market_identity",
            },
            consumers.ArtifactOperationProfile.CAPTURE_DECISION_CONTEXT: {
                "forecast_input",
                "market_identity",
                "decision_book",
                "fee_metadata",
            },
            consumers.ArtifactOperationProfile.CAPTURE_EXECUTION_ATTEMPT: {
                "forecast_input",
                "market_identity",
                "decision_book",
                "post_latency_book",
                "fee_metadata",
            },
            consumers.ArtifactOperationProfile.CAPTURE_PROVIDER_OUTCOME_UNIDENTIFIED: {
                "provider_outcome"
            },
            consumers.ArtifactOperationProfile.CAPTURE_PROVIDER_OUTCOME_IDENTIFIED: {
                "provider_outcome",
                "market_identity",
            },
            consumers.ArtifactOperationProfile.MONITOR_DUE_OPERATION: {
                "position_snapshot",
                "provider_outcome",
            },
            consumers.ArtifactOperationProfile.SETTLEMENT_DUE_OPERATION: {
                "position_snapshot",
                "provider_outcome",
            },
            consumers.ArtifactOperationProfile.SETTLEMENT_FINAL_CORROBORATED: {
                "finalized_block",
                "resolution_transaction",
                "resolution_receipt",
                "condition_resolution_log",
                "payout_state",
                "source_count",
                "position_snapshot",
            },
            consumers.ArtifactOperationProfile.SETTLEMENT_FINAL_XTRACKER_UNAVAILABLE: {
                "finalized_block",
                "resolution_transaction",
                "resolution_receipt",
                "condition_resolution_log",
                "payout_state",
                "position_snapshot",
                "provider_outcome",
            },
        }
        for profile, roles in expected.items():
            self.assertEqual(
                consumers.OPERATION_PROFILE_CONTRACTS[profile].roles,
                frozenset(roles),
            )

    def test_caller_selected_completeness_is_no_longer_representable(self) -> None:
        signature = inspect.signature(
            consumers.V5RuntimeArtifactConsumer.consume_capture
        )
        self.assertNotIn("required_roles", signature.parameters)
        envelope = self.make_envelope(
            phase="capture",
            role="forecast_input",
            data=b'{"forecast":true}\n',
        )
        with mock.patch.object(runtime, "read_verified_artifact") as reader:
            with self.assertRaises(consumers.RuntimeConsumerContractError):
                self.consumer.consume_capture(  # type: ignore[arg-type]
                    (envelope,),
                    ("forecast_input",),
                    self.source_manifest,
                    self.expected_sources,
                )
            with self.assertRaisesRegex(
                consumers.RuntimeConsumerContractError,
                "missing=",
            ):
                self.consumer.consume_capture(
                    consumers.ArtifactOperationProfile.CAPTURE_EXECUTION_ATTEMPT,
                    (envelope,),
                    self.source_manifest,
                    self.expected_sources,
                )
            reader.assert_not_called()

    def test_profile_batch_errors_all_precede_first_artifact_read(self) -> None:
        identity = self.make_envelope(
            phase="capture",
            role="market_identity",
            data=b'{"identity":true}\n',
        )
        duplicate = dataclasses.replace(identity)
        outcome = self.make_envelope(
            phase="capture",
            role="provider_outcome",
            data=b'{"outcome":"available"}\n',
        )
        cases = (
            (
                "missing",
                consumers.ArtifactOperationProfile.CAPTURE_FORECAST,
                (identity,),
                "missing=",
            ),
            (
                "unexpected",
                consumers.ArtifactOperationProfile.CAPTURE_IDENTITY,
                (identity, outcome),
                "unexpected=",
            ),
            (
                "duplicate",
                consumers.ArtifactOperationProfile.CAPTURE_IDENTITY,
                (identity, duplicate),
                "duplicate requested role",
            ),
        )
        with mock.patch.object(runtime, "read_verified_artifact") as reader:
            for label, profile, envelopes, error in cases:
                with self.subTest(label=label):
                    with self.assertRaisesRegex(
                        consumers.RuntimeConsumerContractError,
                        error,
                    ):
                        self.consumer.consume_capture(
                            profile,
                            envelopes,
                            self.source_manifest,
                            self.expected_sources,
                        )
            with self.assertRaisesRegex(
                consumers.RuntimeConsumerContractError,
                "cannot be used for monitor",
            ):
                self.consumer.consume_monitor(
                    consumers.ArtifactOperationProfile.CAPTURE_IDENTITY,
                    (identity,),
                    self.source_manifest,
                    self.expected_sources,
                )
            with self.assertRaisesRegex(
                consumers.RuntimeConsumerContractError,
                "ArtifactOperationProfile",
            ):
                self.consumer.consume_capture(  # type: ignore[arg-type]
                    "CAPTURE_IDENTITY",
                    (identity,),
                    self.source_manifest,
                    self.expected_sources,
                )
            reader.assert_not_called()

    def test_wrong_provenance_and_evidence_class_precede_first_read(self) -> None:
        role_cases = (
            (
                "source_count",
                "settlement",
                "public_polygon_rpc",
                runtime.RAW_EXTERNAL_ARTIFACT,
            ),
            (
                "position_snapshot",
                "monitor",
                "public_gamma_rest",
                runtime.DERIVED_LOCAL_RECORD,
            ),
            (
                "provider_outcome",
                "monitor",
                "derived_v5_provider_outcome",
                runtime.RAW_EXTERNAL_ARTIFACT,
            ),
            (
                "forecast_input",
                "capture",
                "public_clob_rest",
                runtime.RAW_EXTERNAL_ARTIFACT,
            ),
        )
        with mock.patch.object(runtime, "read_verified_artifact") as reader:
            for role, phase, source_type, evidence_class in role_cases:
                with self.subTest(role=role):
                    envelope = self.make_envelope(
                        phase=phase,
                        role=role,
                        data=b'{"invalid":true}\n',
                        source_type=source_type,
                        evidence_class=evidence_class,
                    )
                    profile = {
                        "source_count": (
                            consumers.ArtifactOperationProfile.SETTLEMENT_FINAL_CORROBORATED
                        ),
                        "position_snapshot": (
                            consumers.ArtifactOperationProfile.MONITOR_DUE_OPERATION
                        ),
                        "provider_outcome": (
                            consumers.ArtifactOperationProfile.MONITOR_DUE_OPERATION
                        ),
                        "forecast_input": (
                            consumers.ArtifactOperationProfile.CAPTURE_FORECAST
                        ),
                    }[role]
                    contract = consumers.OPERATION_PROFILE_CONTRACTS[profile]
                    valid_by_role = {
                        item.artifact_role: item
                        for item in self.profile_envelopes(profile)
                    }
                    valid_by_role[role] = envelope
                    with self.assertRaises(runtime.RuntimeArtifactContractError):
                        self.consume(
                            profile,
                            tuple(
                                valid_by_role[item]
                                for item in sorted(contract.roles)
                            ),
                        )
            reader.assert_not_called()

    def test_monitor_book_is_rejected_and_has_no_profile(self) -> None:
        self.assertNotIn("monitor_book", runtime.MONITOR_ROLES)
        self.assertNotIn("monitor_book", runtime.ROLE_PROVENANCE)
        self.assertTrue(
            all(
                "monitor_book" not in contract.roles
                for contract in consumers.OPERATION_PROFILE_CONTRACTS.values()
            )
        )

    def test_xtracker_unavailable_final_profile_requires_outcome_without_count(
        self,
    ) -> None:
        profile = (
            consumers.ArtifactOperationProfile.SETTLEMENT_FINAL_XTRACKER_UNAVAILABLE
        )
        envelopes = self.profile_envelopes(profile)
        roles = {envelope.artifact_role for envelope in envelopes}
        self.assertIn("provider_outcome", roles)
        self.assertNotIn("source_count", roles)
        provider_outcome = next(
            envelope
            for envelope in envelopes
            if envelope.artifact_role == "provider_outcome"
        )
        self.assertEqual(provider_outcome.reason_code, "unavailable")
        result = self.consume(profile, envelopes)
        self.assertEqual({item.reference.role for item in result}, roles)

        source_count = self.make_envelope(
            phase="settlement",
            role="source_count",
            data=b'{"count":123}\n',
        )
        with mock.patch.object(runtime, "read_verified_artifact") as reader:
            with self.assertRaisesRegex(
                consumers.RuntimeConsumerContractError,
                "unexpected=",
            ):
                self.consume(profile, envelopes + (source_count,))
            wrong_outcome = dataclasses.replace(
                provider_outcome,
                reason_code="available",
            )
            wrong_envelopes = tuple(
                wrong_outcome
                if envelope.artifact_role == "provider_outcome"
                else envelope
                for envelope in envelopes
            )
            with self.assertRaisesRegex(
                consumers.RuntimeConsumerContractError,
                "reason_code='unavailable'",
            ):
                self.consume(profile, wrong_envelopes)
            reader.assert_not_called()

    def test_shared_protocol_lock_manifest_enrollment_and_market_mixing_fails(
        self,
    ) -> None:
        profile = consumers.ArtifactOperationProfile.CAPTURE_FORECAST
        envelopes = list(self.profile_envelopes(profile))
        replacements = {
            "protocol_id": {"protocol_id": "different_protocol"},
            "protocol_sha256": {"protocol_sha256": "4" * 64},
            "complete_lock_sha256": {"complete_lock_sha256": "5" * 64},
            "source_manifest_sha256": {"source_manifest_sha256": "6" * 64},
            "enrollment": {
                "enrollment": dataclasses.replace(
                    self.enrollment,
                    xtracker_tracking_id="different_tracking",
                )
            },
            "market_identity": {
                "market_identity": dataclasses.replace(
                    self.market_identity,
                    token_id="987654321",
                )
            },
        }
        with mock.patch.object(runtime, "read_verified_artifact") as reader:
            for label, changes in replacements.items():
                with self.subTest(label=label):
                    mixed = list(envelopes)
                    mixed[1] = dataclasses.replace(mixed[1], **changes)
                    with self.assertRaisesRegex(
                        consumers.RuntimeConsumerContractError,
                        label,
                    ):
                        self.consume(profile, tuple(mixed))
            reader.assert_not_called()

    def test_unidentified_and_identified_market_binding_is_fail_closed(self) -> None:
        unidentified = (
            consumers.ArtifactOperationProfile.CAPTURE_PROVIDER_OUTCOME_UNIDENTIFIED
        )
        outcome = self.profile_envelopes(unidentified)[0]
        self.assertIsNone(outcome.market_identity)
        self.consume(unidentified, (outcome,))

        falsely_identified = dataclasses.replace(
            outcome,
            market_identity=self.market_identity,
        )
        identified = (
            consumers.ArtifactOperationProfile.CAPTURE_PROVIDER_OUTCOME_IDENTIFIED
        )
        identified_envelopes = self.profile_envelopes(identified)
        missing_identity = dataclasses.replace(
            identified_envelopes[0],
            market_identity=None,
        )
        with mock.patch.object(runtime, "read_verified_artifact") as reader:
            with self.assertRaisesRegex(
                consumers.RuntimeConsumerContractError,
                "cannot claim market identity",
            ):
                self.consume(unidentified, (falsely_identified,))
            mixed = (missing_identity,) + identified_envelopes[1:]
            with self.assertRaisesRegex(
                consumers.RuntimeConsumerContractError,
                "requires exact market identity",
            ):
                self.consume(identified, mixed)
            reader.assert_not_called()

    def test_unknown_schema_and_malformed_hash_fail_before_first_read(self) -> None:
        profile = consumers.ArtifactOperationProfile.CAPTURE_IDENTITY
        envelope = self.profile_envelopes(profile)[0]
        candidates = (
            dataclasses.replace(envelope, schema_id="unknown"),
            dataclasses.replace(envelope, schema_version="2"),
            dataclasses.replace(envelope, protocol_sha256="A" * 64),
            dataclasses.replace(envelope, complete_lock_sha256="short"),
            dataclasses.replace(envelope, source_manifest_sha256="g" * 64),
        )
        with mock.patch.object(runtime, "read_verified_artifact") as reader:
            for candidate in candidates:
                with self.subTest(candidate=candidate):
                    with self.assertRaises(runtime.RuntimeArtifactContractError):
                        self.consume(profile, (candidate,))
            reader.assert_not_called()

    def test_complete_envelope_validation_precedes_first_batch_read(self) -> None:
        profile = consumers.ArtifactOperationProfile.CAPTURE_FORECAST
        envelopes = list(self.profile_envelopes(profile))
        invalid = dataclasses.replace(
            envelopes[1],
            source_type="public_clob_rest",
            reference=dataclasses.replace(
                envelopes[1].reference,
                source_type="public_clob_rest",
            ),
        )
        envelopes[1] = invalid
        with mock.patch.object(runtime, "read_verified_artifact") as reader:
            with self.assertRaises(runtime.RuntimeArtifactContractError):
                self.consume(profile, tuple(envelopes))
            reader.assert_not_called()

    def test_artifact_hash_and_length_mismatches_propagate_fail_closed(self) -> None:
        profile = consumers.ArtifactOperationProfile.CAPTURE_IDENTITY
        envelope = self.profile_envelopes(profile)[0]
        wrong_length = dataclasses.replace(
            envelope,
            reference=dataclasses.replace(
                envelope.reference,
                byte_length=envelope.reference.byte_length + 1,
            ),
            byte_length=envelope.byte_length + 1,
        )
        with self.assertRaisesRegex(isolation.ArtifactIntegrityError, "byte length"):
            self.consume(profile, (wrong_length,))

        wrong_hash = "f" * 64
        wrong_path = runtime.CAPTURE_PATH_PREFIX + wrong_hash + ".json"
        wrong_hash_envelope = dataclasses.replace(
            envelope,
            reference=dataclasses.replace(
                envelope.reference,
                relative_path=wrong_path,
                sha256=wrong_hash,
            ),
            canonical_relative_path=wrong_path,
            content_sha256=wrong_hash,
        )
        original_data = self.root.joinpath(
            *envelope.reference.relative_path.split("/")
        ).read_bytes()
        self.write_under_root(wrong_path, original_data)
        with self.assertRaisesRegex(isolation.ArtifactIntegrityError, "SHA-256"):
            self.consume(profile, (wrong_hash_envelope,))

    def test_source_manifest_failure_and_replacement_prevent_artifact_reads(
        self,
    ) -> None:
        profile = consumers.ArtifactOperationProfile.CAPTURE_IDENTITY
        envelopes = self.profile_envelopes(profile)
        with mock.patch.object(runtime, "read_verified_artifact") as reader:
            with self.assertRaises(isolation.SourceIntegrityError):
                self.consume(profile, envelopes, source_manifest=())
            reader.assert_not_called()

        self.write_under_root(self.source_path, b"PAPER_ONLY = False\n")
        with mock.patch.object(runtime, "read_verified_artifact") as reader:
            with self.assertRaises(isolation.SourceIntegrityError):
                self.consume(profile, envelopes)
            reader.assert_not_called()

    def test_hostile_canaries_for_all_phases_read_only_explicit_root(self) -> None:
        profiles = (
            consumers.ArtifactOperationProfile.CAPTURE_IDENTITY,
            consumers.ArtifactOperationProfile.MONITOR_DUE_OPERATION,
            consumers.ArtifactOperationProfile.SETTLEMENT_DUE_OPERATION,
        )
        for profile in profiles:
            with self.subTest(profile=profile.value):
                envelopes = self.profile_envelopes(profile)
                external_records: list[tuple[Path, bytes, str, os.stat_result]] = []
                for envelope in envelopes:
                    external_data = (
                        f'{{"origin":"external-{envelope.artifact_role}"}}\n'
                    ).encode()
                    external = self.write_under_outside(
                        envelope.reference.relative_path,
                        external_data,
                    )
                    external_records.append(
                        (
                            external,
                            external_data,
                            hashlib.sha256(external_data).hexdigest(),
                            external.stat(),
                        )
                    )
                result = self.consume(profile, envelopes)
                self.assertTrue(
                    all(
                        artifact.parsed_object["profile"] == profile.value
                        for artifact in result
                    )
                )
                for external, data, digest, before in external_records:
                    self.assertEqual(external.read_bytes(), data)
                    self.assertEqual(
                        hashlib.sha256(external.read_bytes()).hexdigest(),
                        digest,
                    )
                    after = external.stat()
                    self.assertEqual(after.st_size, before.st_size)
                    self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
                    self.assertEqual(after.st_ino, before.st_ino)

    def test_cwd_and_home_decoys_cannot_change_explicit_root_reads(self) -> None:
        profile = consumers.ArtifactOperationProfile.CAPTURE_IDENTITY
        envelope = self.profile_envelopes(profile)[0]
        decoy = self.write_under_outside(
            envelope.reference.relative_path,
            b'{"origin":"cwd-home-decoy"}\n',
        )
        original_cwd = Path.cwd()
        try:
            os.chdir(self.outside)
            with mock.patch.dict(
                os.environ,
                {"HOME": str(self.outside)},
                clear=False,
            ):
                result = self.consume(profile, (envelope,))
        finally:
            os.chdir(original_cwd)
        self.assertEqual(result[0].raw_bytes, self.root.joinpath(
            *envelope.reference.relative_path.split("/")
        ).read_bytes())
        self.assertEqual(decoy.read_bytes(), b'{"origin":"cwd-home-decoy"}\n')

    def test_intermediate_and_final_symlink_attacks_fail_closed(self) -> None:
        external_data = b'{"external":"symlink"}\n'
        external = self.outside / "symlink.json"
        external.write_bytes(external_data)
        digest = hashlib.sha256(external_data).hexdigest()
        parent = self.root.joinpath(
            *runtime.CAPTURE_PATH_PREFIX.rstrip("/").split("/")[:-1]
        )
        parent.mkdir(parents=True)
        link = parent / "capture"
        try:
            link.symlink_to(self.outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        relative_path = runtime.CAPTURE_PATH_PREFIX + digest + ".json"
        reference = isolation.ArtifactReference(
            role="market_identity",
            relative_path=relative_path,
            sha256=digest,
            byte_length=len(external_data),
            source_type="public_gamma_rest",
        )
        envelope = dataclasses.replace(
            self.make_envelope(
                phase="capture",
                role="market_identity",
                data=external_data,
                write=False,
            ),
            reference=reference,
            canonical_relative_path=relative_path,
            content_sha256=digest,
            byte_length=len(external_data),
        )
        with self.assertRaises(isolation.RootIsolationError):
            self.consume(
                consumers.ArtifactOperationProfile.CAPTURE_IDENTITY,
                (envelope,),
            )
        self.assertEqual(external.read_bytes(), external_data)

        final_data = b'{"external":"final-file-symlink"}\n'
        final_external = self.outside / "final-file-symlink.json"
        final_external.write_bytes(final_data)
        position = self.make_envelope(
            phase="monitor",
            role="position_snapshot",
            data=final_data,
            write=False,
        )
        final_local = self.root.joinpath(
            *position.reference.relative_path.split("/")
        )
        final_local.parent.mkdir(parents=True, exist_ok=True)
        final_local.symlink_to(final_external)
        provider_outcome = self.make_envelope(
            phase="monitor",
            role="provider_outcome",
            data=b'{"provider":"available"}\n',
        )
        with self.assertRaises(isolation.ArtifactIntegrityError):
            self.consume(
                consumers.ArtifactOperationProfile.MONITOR_DUE_OPERATION,
                (position, provider_outcome),
            )
        self.assertEqual(final_external.read_bytes(), final_data)

    def test_deep_json_integrity_boundary_survives_consumer_profiles(self) -> None:
        recursion_limit = 300
        data = (
            '{"nested":' * recursion_limit + "{}" + "}" * recursion_limit
        ).encode()
        envelope = self.make_envelope(
            phase="capture",
            role="market_identity",
            data=data,
        )
        original_limit = sys.getrecursionlimit()
        try:
            sys.setrecursionlimit(recursion_limit)
            with self.assertRaises(runtime.RuntimeArtifactIntegrityError) as caught:
                self.consume(
                    consumers.ArtifactOperationProfile.CAPTURE_IDENTITY,
                    (envelope,),
                )
        finally:
            sys.setrecursionlimit(original_limit)
        self.assertIsInstance(caught.exception.__cause__, RecursionError)

    def test_results_are_immutable_and_retain_envelope_bytes_and_hash(self) -> None:
        profile = consumers.ArtifactOperationProfile.SETTLEMENT_DUE_OPERATION
        envelopes = self.profile_envelopes(profile)
        result = self.consume(profile, envelopes)
        artifact = result[0]
        self.assertIsInstance(result, tuple)
        self.assertIsInstance(artifact.parsed_object, MappingProxyType)
        with self.assertRaises(TypeError):
            artifact.parsed_object["added"] = True  # type: ignore[index]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            artifact.envelope.protocol_id = "changed"  # type: ignore[misc]
        self.assertIs(artifact.envelope, envelopes[0])
        self.assertIs(artifact.reference, envelopes[0].reference)
        self.assertEqual(artifact.raw_bytes, self.root.joinpath(
            *artifact.reference.relative_path.split("/")
        ).read_bytes())
        self.assertEqual(
            artifact.sha256,
            hashlib.sha256(artifact.raw_bytes).hexdigest(),
        )

    def test_construction_and_all_inputs_are_explicit(self) -> None:
        with self.assertRaises(TypeError):
            consumers.V5RuntimeArtifactConsumer()  # type: ignore[call-arg]
        with self.assertRaises(consumers.RuntimeConsumerContractError):
            consumers.V5RuntimeArtifactConsumer(self.root)  # type: ignore[arg-type]
        signature = inspect.signature(consumers.V5RuntimeArtifactConsumer)
        self.assertEqual(
            signature.parameters["paths"].default,
            inspect.Parameter.empty,
        )
        for method_name in (
            "consume_capture",
            "consume_monitor",
            "consume_settlement",
        ):
            method_signature = inspect.signature(
                getattr(consumers.V5RuntimeArtifactConsumer, method_name)
            )
            for parameter in (
                "profile",
                "envelopes",
                "source_manifest",
                "expected_source_paths",
            ):
                self.assertEqual(
                    method_signature.parameters[parameter].default,
                    inspect.Parameter.empty,
                )

    def test_consumer_source_has_no_direct_read_or_unsafe_capability(self) -> None:
        source = CONSUMER_SOURCE.read_text(encoding="utf-8")
        lowered = source.lower()
        self.assertNotIn("/data/workspace/polymarket-research", lowered)
        self.assertIn("source-only", lowered)
        self.assertIn("does not deploy, activate, or authorize v5", lowered)
        self.assertNotIn("read_verified_artifact", source)
        self.assertNotIn("verify_source_manifest", source)
        self.assertEqual(source.count("self._gateway.read_capture_json"), 1)
        self.assertEqual(source.count("self._gateway.read_monitor_json"), 1)
        self.assertEqual(source.count("self._gateway.read_settlement_json"), 1)

        tree = ast.parse(source)
        imported_roots: set[str] = set()
        forbidden_calls: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == "open":
                    forbidden_calls.append("open")
                elif isinstance(node.func, ast.Attribute) and node.func.attr in {
                    "open",
                    "read_bytes",
                    "read_text",
                    "write_bytes",
                    "write_text",
                }:
                    forbidden_calls.append(node.func.attr)
        self.assertTrue(
            imported_roots.isdisjoint(
                {
                    "asyncio",
                    "http",
                    "json",
                    "os",
                    "pathlib",
                    "requests",
                    "shutil",
                    "socket",
                    "subprocess",
                    "urllib",
                }
            )
        )
        self.assertEqual(forbidden_calls, [])

    def test_all_v5_imports_resolve_to_this_exact_checkout(self) -> None:
        for module in (consumers, runtime, isolation):
            module_path = Path(module.__file__).resolve()
            self.assertEqual(module_path.parent, SOURCE_ROOT)
            self.assertNotIn(
                "/data/workspace/polymarket-research",
                module_path.as_posix(),
            )

    def test_all_runtime_test_roots_are_temporary_and_nonproduction(self) -> None:
        self.assertTrue(str(self.root).startswith(tempfile.gettempdir()))
        self.assertTrue(str(self.outside).startswith(tempfile.gettempdir()))
        self.assertNotEqual(self.root, SOURCE_ROOT)
        self.assertNotIn("data/workspace/polymarket-research", self.root.as_posix())
        self.assertNotEqual(self.root, self.outside)


if __name__ == "__main__":
    unittest.main()
