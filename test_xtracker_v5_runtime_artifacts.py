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
from typing import Callable
from unittest import mock

import xtracker_v5_isolation as isolation
import xtracker_v5_runtime_artifacts as runtime


SOURCE_ROOT = Path(__file__).resolve().parent
RUNTIME_SOURCE = SOURCE_ROOT / "xtracker_v5_runtime_artifacts.py"
_DEFAULT = object()
_HASH_A = "1" * 64
_HASH_B = "2" * 64
_HASH_C = "3" * 64


class V5RuntimeArtifactGatewayTests(unittest.TestCase):
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
        self.gateway = runtime.V5RuntimeArtifactGateway(self.paths)

        self.source_path = "src/future_v5_consumer.py"
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
        relative_path: str | None = None,
        write: bool = True,
        schema_id: str = runtime.ARTIFACT_ENVELOPE_SCHEMA_ID,
        schema_version: str = runtime.ARTIFACT_ENVELOPE_SCHEMA_VERSION,
        protocol_id: str = "xtracker_forward_v5_test",
        protocol_sha256: str = _HASH_A,
        complete_lock_sha256: str = _HASH_B,
        source_manifest_sha256: str = _HASH_C,
        enrollment: object = _DEFAULT,
        market_identity: object = _DEFAULT,
        reason_code: object = _DEFAULT,
    ) -> runtime.ArtifactEnvelope:
        provenance = runtime.ROLE_PROVENANCE.get(role)
        selected_source = (
            source_type
            if source_type is not None
            else provenance.source_type if provenance is not None else "invalid_source"
        )
        selected_evidence_class = (
            evidence_class
            if evidence_class is not None
            else provenance.evidence_class if provenance is not None else "invalid_class"
        )
        digest = hashlib.sha256(data).hexdigest()
        if relative_path is None:
            relative_path = runtime.artifact_path_prefix(phase, role) + digest + ".json"
        reference = isolation.ArtifactReference(
            role=role,
            relative_path=relative_path,
            sha256=digest,
            byte_length=len(data),
            source_type=selected_source,
        )
        if write:
            self.write_under_root(relative_path, data)
        selected_reason = (
            "available"
            if reason_code is _DEFAULT and role == "provider_outcome"
            else None if reason_code is _DEFAULT else reason_code
        )
        return runtime.ArtifactEnvelope(
            reference=reference,
            schema_id=schema_id,
            schema_version=schema_version,
            artifact_role=role,
            evidence_class=selected_evidence_class,
            protocol_id=protocol_id,
            protocol_sha256=protocol_sha256,
            complete_lock_sha256=complete_lock_sha256,
            source_manifest_sha256=source_manifest_sha256,
            source_type=selected_source,
            enrollment=(
                self.enrollment if enrollment is _DEFAULT else enrollment
            ),
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
            reason_code=selected_reason,  # type: ignore[arg-type]
        )

    def read_envelope(
        self,
        phase: str,
        envelope: runtime.ArtifactEnvelope,
    ) -> runtime.VerifiedJSONArtifact:
        readers: dict[
            str,
            Callable[
                [
                    runtime.ArtifactEnvelope,
                    tuple[isolation.SourceDigest, ...],
                    tuple[str, ...],
                ],
                runtime.VerifiedJSONArtifact,
            ],
        ] = {
            "capture": self.gateway.read_capture_json,
            "monitor": self.gateway.read_monitor_json,
            "settlement": self.gateway.read_settlement_json,
        }
        return readers[phase](
            envelope,
            self.source_manifest,
            self.expected_sources,
        )

    def test_successful_exact_capture_monitor_and_settlement_reads(self) -> None:
        cases = (
            (
                "capture",
                "decision_book",
                b'{"book":{"asks":[[0.25,5]],"bids":[]},"paper":true}\n',
            ),
            (
                "monitor",
                "position_snapshot",
                b'{"position":{"shares":5},"paper":true}\n',
            ),
            (
                "settlement",
                "resolution_receipt",
                b'{"receipt":{"status":"finalized"},"paper":true}\n',
            ),
        )
        with mock.patch.object(
            runtime,
            "read_verified_artifact",
            wraps=runtime.read_verified_artifact,
        ) as exact_reader:
            for phase, role, data in cases:
                with self.subTest(phase=phase):
                    envelope = self.make_envelope(
                        phase=phase,
                        role=role,
                        data=data,
                    )
                    record = self.read_envelope(phase, envelope)
                    self.assertEqual(record.phase, phase)
                    self.assertIs(record.envelope, envelope)
                    self.assertIs(record.reference, envelope.reference)
                    self.assertEqual(record.raw_bytes, data)
                    self.assertEqual(record.sha256, hashlib.sha256(data).hexdigest())
                    self.assertTrue(record.parsed_object["paper"])
            self.assertEqual(exact_reader.call_count, 3)

    def test_result_is_deeply_read_only_and_retains_exact_bytes(self) -> None:
        data = b'{"nested":{"values":[1,{"two":2}]}}\n'
        envelope = self.make_envelope(
            phase="capture",
            role="forecast_input",
            data=data,
        )
        record = self.read_envelope("capture", envelope)

        self.assertIsInstance(record.parsed_object, MappingProxyType)
        nested = record.parsed_object["nested"]
        self.assertIsInstance(nested, MappingProxyType)
        self.assertIsInstance(nested["values"], tuple)  # type: ignore[index]
        with self.assertRaises(TypeError):
            record.parsed_object["added"] = True  # type: ignore[index]
        with self.assertRaises(TypeError):
            nested["added"] = True  # type: ignore[index]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            record.phase = "monitor"  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            record.envelope.protocol_id = "changed"  # type: ignore[misc]
        self.assertEqual(record.raw_bytes, data)

    def test_source_verification_failure_prevents_artifact_read(self) -> None:
        envelope = self.make_envelope(
            phase="capture",
            role="decision_book",
            data=b'{"paper":true}\n',
        )
        failure = isolation.SourceIntegrityError("forced source failure")
        with (
            mock.patch.object(
                runtime,
                "verify_source_manifest",
                side_effect=failure,
            ) as verifier,
            mock.patch.object(runtime, "read_verified_artifact") as artifact_reader,
        ):
            with self.assertRaisesRegex(isolation.SourceIntegrityError, "forced"):
                self.gateway.read_capture_json(
                    envelope,
                    self.source_manifest,
                    self.expected_sources,
                )
            verifier.assert_called_once_with(
                self.paths,
                self.source_manifest,
                self.expected_sources,
            )
            artifact_reader.assert_not_called()

    def test_duplicate_and_mismatched_source_membership_fail_before_read(self) -> None:
        envelope = self.make_envelope(
            phase="monitor",
            role="position_snapshot",
            data=b'{"position":{}}\n',
        )
        with mock.patch.object(runtime, "read_verified_artifact") as artifact_reader:
            with self.assertRaisesRegex(
                isolation.SourceIntegrityError,
                "duplicate manifest",
            ):
                self.gateway.read_monitor_json(
                    envelope,
                    self.source_manifest + self.source_manifest,
                    self.expected_sources,
                )
            with self.assertRaisesRegex(
                isolation.SourceIntegrityError,
                "membership mismatch",
            ):
                self.gateway.read_monitor_json(
                    envelope,
                    self.source_manifest,
                    ("src/different_consumer.py",),
                )
            artifact_reader.assert_not_called()

    def test_modified_artifact_hash_and_length_mismatch_are_rejected(self) -> None:
        data = b'{"value":1}\n'
        envelope = self.make_envelope(
            phase="capture",
            role="fee_metadata",
            data=data,
        )
        self.write_under_root(
            envelope.reference.relative_path,
            b'{"value":2}\n',
        )
        with self.assertRaisesRegex(isolation.ArtifactIntegrityError, "SHA-256"):
            self.read_envelope("capture", envelope)

        self.write_under_root(envelope.reference.relative_path, data)
        wrong_reference = dataclasses.replace(
            envelope.reference,
            byte_length=envelope.reference.byte_length + 1,
        )
        wrong_envelope = dataclasses.replace(
            envelope,
            reference=wrong_reference,
            byte_length=wrong_reference.byte_length,
        )
        with self.assertRaisesRegex(isolation.ArtifactIntegrityError, "byte length"):
            self.read_envelope("capture", wrong_envelope)

    def test_envelope_reference_binding_mismatch_fails_before_io(self) -> None:
        envelope = self.make_envelope(
            phase="capture",
            role="forecast_input",
            data=b'{"forecast":1}\n',
        )
        mismatches = (
            dataclasses.replace(envelope, artifact_role="market_identity"),
            dataclasses.replace(envelope, source_type="public_gamma_rest"),
            dataclasses.replace(envelope, byte_length=envelope.byte_length + 1),
            dataclasses.replace(envelope, content_sha256="f" * 64),
            dataclasses.replace(
                envelope,
                canonical_relative_path=runtime.CAPTURE_PATH_PREFIX
                + "f" * 64
                + ".json",
            ),
        )
        with mock.patch.object(runtime, "read_verified_artifact") as reader:
            for mismatch in mismatches:
                with self.subTest(field=mismatch):
                    with self.assertRaises(runtime.RuntimeArtifactContractError):
                        self.gateway.read_capture_json(
                            mismatch,
                            self.source_manifest,
                            self.expected_sources,
                        )
            reader.assert_not_called()

    def test_every_role_enforces_exact_source_and_evidence_class(self) -> None:
        role_phases = {
            "forecast_input": "capture",
            "market_identity": "capture",
            "decision_book": "capture",
            "post_latency_book": "capture",
            "fee_metadata": "capture",
            "position_snapshot": "monitor",
            "provider_outcome": "monitor",
            "finalized_block": "settlement",
            "resolution_transaction": "settlement",
            "resolution_receipt": "settlement",
            "condition_resolution_log": "settlement",
            "payout_state": "settlement",
            "source_count": "settlement",
        }
        all_sources = {
            "public_x_record",
            "public_gamma_rest",
            "public_clob_rest",
            "public_polygon_rpc",
            "derived_v5_lifecycle",
            "derived_v5_provider_outcome",
            "local_cache",
        }
        for role, phase in role_phases.items():
            expected = runtime.ROLE_PROVENANCE[role]
            valid = self.make_envelope(
                phase=phase,
                role=role,
                data=f'{{"role":"{role}"}}\n'.encode(),
            )
            self.assertIs(
                runtime.validate_artifact_envelope(phase, valid),
                valid.reference,
            )
            for wrong_source in sorted(all_sources - {expected.source_type}):
                with self.subTest(role=role, wrong_source=wrong_source):
                    invalid = self.make_envelope(
                        phase=phase,
                        role=role,
                        data=b'{"invalid-source":true}\n',
                        source_type=wrong_source,
                        write=False,
                    )
                    with self.assertRaises(runtime.RuntimeArtifactContractError):
                        runtime.validate_artifact_envelope(phase, invalid)
            wrong_class = (
                runtime.DERIVED_LOCAL_RECORD
                if expected.evidence_class == runtime.RAW_EXTERNAL_ARTIFACT
                else runtime.RAW_EXTERNAL_ARTIFACT
            )
            invalid_class = self.make_envelope(
                phase=phase,
                role=role,
                data=b'{"invalid-class":true}\n',
                evidence_class=wrong_class,
                write=False,
            )
            with self.assertRaises(runtime.RuntimeArtifactContractError):
                runtime.validate_artifact_envelope(phase, invalid_class)

    def test_source_count_and_derived_roles_have_no_provenance_fallback(self) -> None:
        self.assertEqual(
            runtime.ROLE_PROVENANCE["source_count"],
            runtime.RoleProvenance(
                runtime.RAW_EXTERNAL_ARTIFACT,
                "public_x_record",
            ),
        )
        self.assertEqual(
            runtime.ROLE_PROVENANCE["position_snapshot"],
            runtime.RoleProvenance(
                runtime.DERIVED_LOCAL_RECORD,
                "derived_v5_lifecycle",
            ),
        )
        self.assertEqual(
            runtime.ROLE_PROVENANCE["provider_outcome"],
            runtime.RoleProvenance(
                runtime.DERIVED_LOCAL_RECORD,
                "derived_v5_provider_outcome",
            ),
        )
        self.assertNotIn("monitor_book", runtime.ROLE_PROVENANCE)
        self.assertNotIn("monitor_book", runtime.MONITOR_ROLES)

    def test_unknown_schema_empty_identity_and_malformed_hashes_fail_closed(
        self,
    ) -> None:
        envelope = self.make_envelope(
            phase="capture",
            role="market_identity",
            data=b'{"identity":true}\n',
        )
        invalid = (
            dataclasses.replace(envelope, schema_id="unknown"),
            dataclasses.replace(envelope, schema_version="2"),
            dataclasses.replace(envelope, protocol_id=""),
            dataclasses.replace(envelope, protocol_sha256="A" * 64),
            dataclasses.replace(envelope, complete_lock_sha256="short"),
            dataclasses.replace(envelope, source_manifest_sha256="g" * 64),
        )
        with mock.patch.object(runtime, "read_verified_artifact") as reader:
            for candidate in invalid:
                with self.subTest(candidate=candidate):
                    with self.assertRaises(runtime.RuntimeArtifactContractError):
                        self.gateway.read_capture_json(
                            candidate,
                            self.source_manifest,
                            self.expected_sources,
                        )
            reader.assert_not_called()

    def test_typed_identity_and_timestamp_syntax_are_validated(self) -> None:
        envelope = self.make_envelope(
            phase="capture",
            role="market_identity",
            data=b'{"identity":true}\n',
        )
        invalid_enrollment = dataclasses.replace(
            self.enrollment,
            window_start_utc="2026-07-27 00:00:00",
        )
        invalid_market = dataclasses.replace(
            self.market_identity,
            outcome=" Up ",
        )
        invalid = (
            dataclasses.replace(envelope, enrollment={"platform": "x"}),
            dataclasses.replace(envelope, enrollment=invalid_enrollment),
            dataclasses.replace(envelope, market_identity={"token_id": "1"}),
            dataclasses.replace(envelope, market_identity=invalid_market),
            dataclasses.replace(
                envelope,
                semantic_observed_at_utc="not-a-timestamp",
            ),
        )
        for candidate in invalid:
            with self.subTest(candidate=candidate):
                with self.assertRaises(runtime.RuntimeArtifactContractError):
                    runtime.validate_artifact_envelope("capture", candidate)

    def test_phase_role_and_content_addressed_path_fail_before_io(self) -> None:
        data = b'{"paper":true}\n'
        digest = hashlib.sha256(data).hexdigest()
        valid = self.make_envelope(
            phase="capture",
            role="forecast_input",
            data=data,
        )
        wrong_phase = self.make_envelope(
            phase="monitor",
            role="position_snapshot",
            data=data,
            write=False,
        )
        non_content_addressed = self.make_envelope(
            phase="capture",
            role="forecast_input",
            data=data,
            relative_path=runtime.CAPTURE_PATH_PREFIX + "latest.json",
            write=False,
        )
        traversal = self.make_envelope(
            phase="capture",
            role="forecast_input",
            data=data,
            relative_path=runtime.CAPTURE_PATH_PREFIX
            + "../"
            + digest
            + ".json",
            write=False,
        )
        absolute = self.make_envelope(
            phase="capture",
            role="forecast_input",
            data=data,
            relative_path="/" + runtime.CAPTURE_PATH_PREFIX + digest + ".json",
            write=False,
        )
        invalid_paths = (
            runtime.CAPTURE_PATH_PREFIX,
            runtime.CAPTURE_PATH_PREFIX + "nested\\" + digest + ".json",
            runtime.CAPTURE_PATH_PREFIX + "nested//" + digest + ".json",
            runtime.CAPTURE_PATH_PREFIX + "./" + digest + ".json",
            runtime.CAPTURE_PATH_PREFIX + "%2e%2e/" + digest + ".json",
            runtime.CAPTURE_PATH_PREFIX + "bad name/" + digest + ".json",
            runtime.CAPTURE_PATH_PREFIX + digest + ".txt",
        )
        aliases = tuple(
            self.make_envelope(
                phase="capture",
                role="forecast_input",
                data=data,
                relative_path=relative_path,
                write=False,
            )
            for relative_path in invalid_paths
        )
        path_object = dataclasses.replace(
            valid,
            reference=dataclasses.replace(
                valid.reference,
                relative_path=Path(valid.reference.relative_path),
            ),
            canonical_relative_path=Path(valid.canonical_relative_path),
        )
        with mock.patch.object(runtime, "read_verified_artifact") as reader:
            for phase, candidate in (
                ("capture", wrong_phase),
                ("capture", non_content_addressed),
                ("capture", traversal),
                ("capture", absolute),
                *((("capture", alias) for alias in aliases)),
                ("capture", path_object),
            ):
                with self.subTest(candidate=candidate):
                    with self.assertRaises(runtime.RuntimeArtifactContractError):
                        self.gateway._read_phase_json(  # type: ignore[attr-defined]
                            phase,
                            candidate,
                            self.source_manifest,
                            self.expected_sources,
                        )
            self.assertIs(
                runtime.validate_artifact_envelope("capture", valid),
                valid.reference,
            )
            reader.assert_not_called()

    def test_non_envelope_is_rejected_before_io(self) -> None:
        with (
            mock.patch.object(runtime, "verify_source_manifest") as verifier,
            mock.patch.object(runtime, "read_verified_artifact") as artifact_reader,
        ):
            with self.assertRaisesRegex(
                runtime.RuntimeArtifactContractError,
                "ArtifactEnvelope",
            ):
                self.gateway.read_capture_json(  # type: ignore[arg-type]
                    {"reference": "anything"},
                    self.source_manifest,
                    self.expected_sources,
                )
            verifier.assert_not_called()
            artifact_reader.assert_not_called()

    def test_invalid_json_forms_fail_closed(self) -> None:
        invalid_documents = {
            "utf8-bom": b'\xef\xbb\xbf{"value":1}\n',
            "invalid-utf8": b'{"value":"\xff"}\n',
            "top-duplicate": b'{"value":1,"value":2}\n',
            "nested-duplicate": b'{"nested":{"value":1,"value":2}}\n',
            "nan": b'{"value":NaN}\n',
            "infinity": b'{"value":Infinity}\n',
            "negative-infinity": b'{"value":-Infinity}\n',
            "float-overflow": b'{"value":1e9999}\n',
            "trailing-comma": b'{"value":1,}\n',
            "two-documents": b'{"value":1}{"value":2}\n',
        }
        for label, data in invalid_documents.items():
            with self.subTest(label=label):
                envelope = self.make_envelope(
                    phase="capture",
                    role="forecast_input",
                    data=data,
                )
                with self.assertRaises(runtime.RuntimeArtifactIntegrityError):
                    self.read_envelope("capture", envelope)

    def test_non_object_json_top_levels_fail_closed(self) -> None:
        for data in (b"[]\n", b'"text"\n', b"null\n", b"true\n", b"42\n"):
            with self.subTest(data=data):
                envelope = self.make_envelope(
                    phase="monitor",
                    role="position_snapshot",
                    data=data,
                )
                with self.assertRaisesRegex(
                    runtime.RuntimeArtifactIntegrityError,
                    "top level",
                ):
                    self.read_envelope("monitor", envelope)

    def test_duplicate_key_escape_alias_is_rejected_at_nested_depth(self) -> None:
        envelope = self.make_envelope(
            phase="settlement",
            role="condition_resolution_log",
            data=b'{"outer":{"key":1,"\\u006bey":2}}\n',
        )
        with self.assertRaisesRegex(
            runtime.RuntimeArtifactIntegrityError,
            "duplicate key",
        ):
            self.read_envelope("settlement", envelope)

    def test_deep_valid_json_freeze_recursion_fails_closed(self) -> None:
        recursion_limit = 300
        data = (
            '{"nested":' * recursion_limit + "{}" + "}" * recursion_limit
        ).encode("utf-8")
        envelope = self.make_envelope(
            phase="capture",
            role="forecast_input",
            data=data,
        )

        original_limit = sys.getrecursionlimit()
        try:
            sys.setrecursionlimit(recursion_limit)
            with self.assertRaisesRegex(
                runtime.RuntimeArtifactIntegrityError,
                "immutable conversion",
            ) as caught:
                self.read_envelope("capture", envelope)
        finally:
            sys.setrecursionlimit(original_limit)
        self.assertIsInstance(caught.exception.__cause__, RecursionError)

    def test_absolute_and_symlink_escape_cannot_reach_external_artifact(self) -> None:
        external_data = b'{"external":"must-not-be-read"}\n'
        external = self.outside / "escape.json"
        external.write_bytes(external_data)
        digest = hashlib.sha256(external_data).hexdigest()
        capture_parent = self.root.joinpath(
            *runtime.CAPTURE_PATH_PREFIX.rstrip("/").split("/")[:-1]
        )
        capture_parent.mkdir(parents=True, exist_ok=True)
        capture_link = capture_parent / "capture"
        try:
            capture_link.symlink_to(self.outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

        envelope = self.make_envelope(
            phase="capture",
            role="decision_book",
            data=external_data,
            relative_path=runtime.CAPTURE_PATH_PREFIX + digest + ".json",
            write=False,
        )
        before = external.stat()
        with self.assertRaisesRegex(isolation.RootIsolationError, "symlink"):
            self.read_envelope("capture", envelope)
        self.assertEqual(external.read_bytes(), external_data)
        after = external.stat()
        self.assertEqual(after.st_size, before.st_size)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)

    def test_hostile_external_canary_is_not_fallback_for_missing_local_artifact(
        self,
    ) -> None:
        data = b'{"outcome":"external-canary"}\n'
        envelope = self.make_envelope(
            phase="settlement",
            role="resolution_receipt",
            data=data,
            write=False,
        )
        external = self.write_under_outside(
            envelope.reference.relative_path,
            data,
        )
        before = external.stat()
        with self.assertRaisesRegex(isolation.ArtifactIntegrityError, "missing"):
            self.read_envelope("settlement", envelope)
        self.assertEqual(external.read_bytes(), data)
        after = external.stat()
        self.assertEqual(after.st_size, before.st_size)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(after.st_ino, before.st_ino)

    def test_construction_requires_explicit_bundle_without_default_root(self) -> None:
        with self.assertRaises(TypeError):
            runtime.V5RuntimeArtifactGateway()  # type: ignore[call-arg]
        with self.assertRaisesRegex(
            runtime.RuntimeArtifactContractError,
            "explicit V5PathBundle",
        ):
            runtime.V5RuntimeArtifactGateway(self.root)  # type: ignore[arg-type]
        signature = inspect.signature(runtime.V5RuntimeArtifactGateway)
        self.assertEqual(
            signature.parameters["paths"].default,
            inspect.Parameter.empty,
        )

    def test_runtime_module_has_no_unsafe_capability_or_production_root(self) -> None:
        source = RUNTIME_SOURCE.read_text(encoding="utf-8")
        lowered = source.lower()
        self.assertNotIn("/data/workspace/polymarket-research", lowered)
        self.assertIn("paper-only", lowered)
        self.assertIn("integrity boundary only", lowered)

        tree = ast.parse(source)
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])
        self.assertTrue(
            imported_roots.isdisjoint(
                {
                    "asyncio",
                    "http",
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
        for forbidden_call in (
            ".read_bytes(",
            ".read_text(",
            ".write_bytes(",
            ".write_text(",
            " open(",
        ):
            self.assertNotIn(forbidden_call, source)

    def test_all_runtime_test_roots_are_temporary_and_nonproduction(self) -> None:
        self.assertTrue(str(self.root).startswith(tempfile.gettempdir()))
        self.assertTrue(str(self.outside).startswith(tempfile.gettempdir()))
        self.assertNotEqual(self.root, SOURCE_ROOT)
        self.assertNotIn("data/workspace/polymarket-research", self.root.as_posix())
        self.assertFalse(os.path.lexists(self.paths.output))


if __name__ == "__main__":
    unittest.main()
