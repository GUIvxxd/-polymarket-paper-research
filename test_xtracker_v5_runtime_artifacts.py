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


class V5RuntimeArtifactGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = tempfile.TemporaryDirectory()
        self.sandbox_root = Path(self.sandbox.name).resolve()
        self.root = self.sandbox_root / "explicit-v5-root"
        self.root.mkdir()
        self.outside = self.sandbox_root / "canonical-looking-external-root"
        self.outside.mkdir()
        self.paths = isolation.V5PathBundle.from_root(self.root)
        self.gateway = runtime.V5RuntimeArtifactGateway(self.paths)

        self.source_path = "src/future_v5_consumer.py"
        self.write_under_root(self.source_path, b"PAPER_ONLY = True\n")
        self.expected_sources = (self.source_path,)
        self.source_manifest = isolation.build_source_manifest(
            self.paths,
            self.expected_sources,
        )

    def tearDown(self) -> None:
        self.sandbox.cleanup()

    def write_under_root(self, relative_path: str, data: bytes) -> Path:
        target = self.root.joinpath(*relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def make_reference(
        self,
        *,
        phase: str,
        role: str,
        data: bytes,
        name: str,
        source_type: str | None = None,
        write: bool = True,
    ) -> isolation.ArtifactReference:
        prefixes = {
            "capture": runtime.CAPTURE_PATH_PREFIX,
            "monitor": runtime.MONITOR_PATH_PREFIX,
            "settlement": runtime.SETTLEMENT_PATH_PREFIX,
        }
        default_sources = {
            "capture": "public_clob_rest",
            "monitor": "public_gamma_rest",
            "settlement": "public_polygon_rpc",
        }
        relative_path = prefixes[phase] + name
        if write:
            self.write_under_root(relative_path, data)
        return isolation.ArtifactReference.from_bytes(
            role=role,
            relative_path=relative_path,
            data=data,
            source_type=source_type or default_sources[phase],
        )

    def read_reference(
        self,
        phase: str,
        reference: isolation.ArtifactReference,
    ) -> runtime.VerifiedJSONArtifact:
        readers: dict[
            str,
            Callable[
                [
                    isolation.ArtifactReference,
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
            reference,
            self.source_manifest,
            self.expected_sources,
        )

    def test_successful_exact_capture_monitor_and_settlement_reads(self) -> None:
        cases = (
            (
                "capture",
                "decision_book",
                "public_clob_rest",
                b'{"book":{"asks":[[0.25,5]],"bids":[]},"paper":true}\n',
            ),
            (
                "monitor",
                "position_snapshot",
                "public_gamma_rest",
                b'{"position":{"shares":5},"paper":true}\n',
            ),
            (
                "settlement",
                "resolution_receipt",
                "public_polygon_rpc",
                b'{"receipt":{"status":"finalized"},"paper":true}\n',
            ),
        )
        with mock.patch.object(
            runtime,
            "read_verified_artifact",
            wraps=runtime.read_verified_artifact,
        ) as exact_reader:
            for phase, role, source_type, data in cases:
                with self.subTest(phase=phase):
                    reference = self.make_reference(
                        phase=phase,
                        role=role,
                        data=data,
                        name=f"{phase}.json",
                        source_type=source_type,
                    )
                    record = self.read_reference(phase, reference)
                    self.assertEqual(record.phase, phase)
                    self.assertIs(record.reference, reference)
                    self.assertEqual(record.raw_bytes, data)
                    self.assertEqual(record.sha256, hashlib.sha256(data).hexdigest())
                    self.assertTrue(record.parsed_object["paper"])
            self.assertEqual(exact_reader.call_count, 3)

    def test_result_is_deeply_read_only_and_retains_exact_bytes(self) -> None:
        data = b'{"nested":{"values":[1,{"two":2}]}}\n'
        reference = self.make_reference(
            phase="capture",
            role="forecast_input",
            data=data,
            name="immutable.json",
            source_type="public_x_record",
        )
        record = self.read_reference("capture", reference)

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
        self.assertEqual(record.raw_bytes, data)

    def test_deep_valid_json_freeze_recursion_fails_closed(self) -> None:
        recursion_limit = 300
        data = (
            '{"nested":' * recursion_limit + "{}" + "}" * recursion_limit
        ).encode("utf-8")
        reference = self.make_reference(
            phase="capture",
            role="forecast_input",
            data=data,
            name="deep-valid-object.json",
            source_type="public_x_record",
        )

        original_limit = sys.getrecursionlimit()
        try:
            sys.setrecursionlimit(recursion_limit)
            with self.assertRaisesRegex(
                runtime.RuntimeArtifactIntegrityError,
                "immutable conversion",
            ) as caught:
                self.read_reference("capture", reference)
        finally:
            sys.setrecursionlimit(original_limit)

        self.assertIsInstance(caught.exception.__cause__, RecursionError)

    def test_source_verification_failure_prevents_artifact_read(self) -> None:
        reference = self.make_reference(
            phase="capture",
            role="decision_book",
            data=b'{"paper":true}\n',
            name="source-failure.json",
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
                    reference,
                    self.source_manifest,
                    self.expected_sources,
                )
            verifier.assert_called_once_with(
                self.paths,
                self.source_manifest,
                self.expected_sources,
            )
            artifact_reader.assert_not_called()

    def test_duplicate_manifest_is_not_deduplicated_before_verification(self) -> None:
        reference = self.make_reference(
            phase="monitor",
            role="monitor_book",
            data=b'{"book":{}}\n',
            name="duplicate-source.json",
        )
        duplicate_preserving_manifest = self.source_manifest + self.source_manifest
        with mock.patch.object(runtime, "read_verified_artifact") as artifact_reader:
            with self.assertRaisesRegex(
                isolation.SourceIntegrityError,
                "duplicate manifest",
            ):
                self.gateway.read_monitor_json(
                    reference,
                    duplicate_preserving_manifest,
                    self.expected_sources,
                )
            artifact_reader.assert_not_called()

    def test_expected_source_membership_is_independent_from_manifest(self) -> None:
        reference = self.make_reference(
            phase="settlement",
            role="finalized_block",
            data=b'{"finalized":true}\n',
            name="independent-membership.json",
        )
        independently_supplied_expected = ("src/different_consumer.py",)
        with mock.patch.object(runtime, "read_verified_artifact") as artifact_reader:
            with self.assertRaisesRegex(
                isolation.SourceIntegrityError,
                "membership mismatch",
            ):
                self.gateway.read_settlement_json(
                    reference,
                    self.source_manifest,
                    independently_supplied_expected,
                )
            artifact_reader.assert_not_called()

    def test_modified_artifact_hash_mismatch_is_rejected(self) -> None:
        original = b'{"value":1}\n'
        reference = self.make_reference(
            phase="capture",
            role="fee_metadata",
            data=original,
            name="modified.json",
        )
        self.write_under_root(reference.relative_path, b'{"value":2}\n')

        with self.assertRaisesRegex(isolation.ArtifactIntegrityError, "SHA-256"):
            self.read_reference("capture", reference)

    def test_artifact_length_mismatch_is_rejected(self) -> None:
        data = b'{"value":1}\n'
        valid = self.make_reference(
            phase="monitor",
            role="position_snapshot",
            data=data,
            name="wrong-length.json",
        )
        wrong_length = isolation.ArtifactReference(
            role=valid.role,
            relative_path=valid.relative_path,
            sha256=valid.sha256,
            byte_length=valid.byte_length + 1,
            source_type=valid.source_type,
        )

        with self.assertRaisesRegex(isolation.ArtifactIntegrityError, "byte length"):
            self.read_reference("monitor", wrong_length)

    def test_phase_path_role_and_source_type_are_validated_before_io(self) -> None:
        data = b'{"paper":true}\n'
        digest = hashlib.sha256(data).hexdigest()

        def direct_reference(
            *,
            role: object = "decision_book",
            relative_path: object = runtime.CAPTURE_PATH_PREFIX + "valid.json",
            source_type: object = "public_clob_rest",
        ) -> isolation.ArtifactReference:
            return isolation.ArtifactReference(
                role=role,  # type: ignore[arg-type]
                relative_path=relative_path,  # type: ignore[arg-type]
                sha256=digest,
                byte_length=len(data),
                source_type=source_type,  # type: ignore[arg-type]
            )

        invalid_references = {
            "wrong-phase": direct_reference(
                relative_path=runtime.MONITOR_PATH_PREFIX + "book.json"
            ),
            "prefix-only": direct_reference(
                relative_path=runtime.CAPTURE_PATH_PREFIX
            ),
            "absolute": direct_reference(
                relative_path="/" + runtime.CAPTURE_PATH_PREFIX + "book.json"
            ),
            "traversal": direct_reference(
                relative_path=runtime.CAPTURE_PATH_PREFIX + "../book.json"
            ),
            "backslash": direct_reference(
                relative_path=runtime.CAPTURE_PATH_PREFIX + r"nested\book.json"
            ),
            "double-slash-alias": direct_reference(
                relative_path=runtime.CAPTURE_PATH_PREFIX + "nested//book.json"
            ),
            "dot-alias": direct_reference(
                relative_path=runtime.CAPTURE_PATH_PREFIX + "./book.json"
            ),
            "encoded-alias": direct_reference(
                relative_path=runtime.CAPTURE_PATH_PREFIX + "%2e%2e/book.json"
            ),
            "malformed-space": direct_reference(
                relative_path=runtime.CAPTURE_PATH_PREFIX + "bad name.json"
            ),
            "not-json": direct_reference(
                relative_path=runtime.CAPTURE_PATH_PREFIX + "book.txt"
            ),
            "path-object": direct_reference(
                relative_path=Path(runtime.CAPTURE_PATH_PREFIX + "book.json")
            ),
            "wrong-role": direct_reference(role="resolution_receipt"),
            "non-string-role": direct_reference(role=7),
            "unsupported-source": direct_reference(source_type="local_cache"),
            "blank-source": direct_reference(source_type=" "),
            "non-string-source": direct_reference(source_type=7),
        }

        for label, reference in invalid_references.items():
            with self.subTest(label=label):
                with (
                    mock.patch.object(runtime, "verify_source_manifest") as verifier,
                    mock.patch.object(
                        runtime,
                        "read_verified_artifact",
                    ) as artifact_reader,
                ):
                    with self.assertRaises(runtime.RuntimeArtifactContractError):
                        self.gateway.read_capture_json(
                            reference,
                            self.source_manifest,
                            self.expected_sources,
                        )
                    verifier.assert_not_called()
                    artifact_reader.assert_not_called()

    def test_non_reference_is_rejected_before_io(self) -> None:
        with (
            mock.patch.object(runtime, "verify_source_manifest") as verifier,
            mock.patch.object(runtime, "read_verified_artifact") as artifact_reader,
        ):
            with self.assertRaisesRegex(
                runtime.RuntimeArtifactContractError,
                "ArtifactReference",
            ):
                self.gateway.read_capture_json(  # type: ignore[arg-type]
                    {"relative_path": "anything.json"},
                    self.source_manifest,
                    self.expected_sources,
                )
            verifier.assert_not_called()
            artifact_reader.assert_not_called()

    def test_all_frozen_phase_role_and_public_provenance_allowlists(self) -> None:
        self.assertEqual(
            runtime.CAPTURE_ROLES,
            frozenset(
                {
                    "forecast_input",
                    "decision_book",
                    "post_latency_book",
                    "fee_metadata",
                    "market_identity",
                }
            ),
        )
        self.assertEqual(
            runtime.MONITOR_ROLES,
            frozenset({"monitor_book", "position_snapshot"}),
        )
        self.assertEqual(
            runtime.SETTLEMENT_ROLES,
            frozenset(
                {
                    "finalized_block",
                    "resolution_transaction",
                    "resolution_receipt",
                    "condition_resolution_log",
                    "payout_state",
                    "source_count",
                }
            ),
        )
        for allowlist in (
            runtime.CAPTURE_ROLES,
            runtime.MONITOR_ROLES,
            runtime.SETTLEMENT_ROLES,
            runtime.CAPTURE_SOURCE_TYPES,
            runtime.MONITOR_SOURCE_TYPES,
            runtime.SETTLEMENT_SOURCE_TYPES,
        ):
            self.assertIsInstance(allowlist, frozenset)
            self.assertTrue(allowlist)
        for source_type in (
            runtime.CAPTURE_SOURCE_TYPES
            | runtime.MONITOR_SOURCE_TYPES
            | runtime.SETTLEMENT_SOURCE_TYPES
        ):
            self.assertTrue(source_type.startswith("public_"))

    def test_bom_invalid_utf8_duplicates_nonfinite_and_malformed_json_fail(self) -> None:
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
                reference = self.make_reference(
                    phase="capture",
                    role="forecast_input",
                    data=data,
                    name=f"invalid-{label}.json",
                    source_type="public_x_record",
                )
                with self.assertRaises(runtime.RuntimeArtifactIntegrityError):
                    self.read_reference("capture", reference)

    def test_non_object_json_top_levels_fail_closed(self) -> None:
        non_objects = (
            b"[]\n",
            b'"text"\n',
            b"null\n",
            b"true\n",
            b"42\n",
        )
        for index, data in enumerate(non_objects):
            with self.subTest(data=data):
                reference = self.make_reference(
                    phase="monitor",
                    role="monitor_book",
                    data=data,
                    name=f"non-object-{index}.json",
                )
                with self.assertRaisesRegex(
                    runtime.RuntimeArtifactIntegrityError,
                    "top level",
                ):
                    self.read_reference("monitor", reference)

    def test_duplicate_key_escape_alias_is_rejected_at_nested_depth(self) -> None:
        data = b'{"outer":{"key":1,"\\u006bey":2}}\n'
        reference = self.make_reference(
            phase="settlement",
            role="condition_resolution_log",
            data=data,
            name="escaped-duplicate.json",
        )
        with self.assertRaisesRegex(
            runtime.RuntimeArtifactIntegrityError,
            "duplicate key",
        ):
            self.read_reference("settlement", reference)

    def test_absolute_and_traversal_rejection_cannot_reach_reader(self) -> None:
        external = self.outside / "outside.json"
        external.write_bytes(b'{"external":true}\n')
        data = external.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        unsafe_paths = (
            str(external.resolve()),
            runtime.SETTLEMENT_PATH_PREFIX + "../../outside.json",
        )
        for unsafe_path in unsafe_paths:
            reference = isolation.ArtifactReference(
                role="resolution_receipt",
                relative_path=unsafe_path,
                sha256=digest,
                byte_length=len(data),
                source_type="public_polygon_rpc",
            )
            with self.subTest(path=unsafe_path):
                with mock.patch.object(
                    runtime,
                    "read_verified_artifact",
                ) as artifact_reader:
                    with self.assertRaises(runtime.RuntimeArtifactContractError):
                        self.gateway.read_settlement_json(
                            reference,
                            self.source_manifest,
                            self.expected_sources,
                        )
                    artifact_reader.assert_not_called()
        self.assertEqual(external.read_bytes(), data)

    def test_symlink_escape_is_rejected_by_no_follow_reader(self) -> None:
        external = self.outside / "escape.json"
        external_bytes = b'{"external":"must-not-be-read"}\n'
        external.write_bytes(external_bytes)
        before = external.stat()

        capture_parent = self.root.joinpath(
            *runtime.CAPTURE_PATH_PREFIX.rstrip("/").split("/")[:-1]
        )
        capture_parent.mkdir(parents=True, exist_ok=True)
        capture_link = capture_parent / "capture"
        try:
            capture_link.symlink_to(self.outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

        reference = isolation.ArtifactReference.from_bytes(
            role="decision_book",
            relative_path=runtime.CAPTURE_PATH_PREFIX + external.name,
            data=external_bytes,
            source_type="public_clob_rest",
        )
        with self.assertRaisesRegex(isolation.RootIsolationError, "symlink"):
            self.read_reference("capture", reference)

        self.assertEqual(external.read_bytes(), external_bytes)
        after = external.stat()
        self.assertEqual(after.st_size, before.st_size)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(after.st_ino, before.st_ino)

    def test_hostile_external_canary_is_not_fallback_for_missing_local_artifact(
        self,
    ) -> None:
        relative_path = runtime.SETTLEMENT_PATH_PREFIX + "canonical-proof.json"
        external = self.outside.joinpath(*relative_path.split("/"))
        external.parent.mkdir(parents=True)
        external_bytes = b'{"outcome":"external-canary"}\n'
        external.write_bytes(external_bytes)
        before = external.stat()
        reference = isolation.ArtifactReference.from_bytes(
            role="resolution_receipt",
            relative_path=relative_path,
            data=external_bytes,
            source_type="public_polygon_rpc",
        )

        with self.assertRaisesRegex(isolation.ArtifactIntegrityError, "missing"):
            self.read_reference("settlement", reference)

        self.assertFalse(self.root.joinpath(*relative_path.split("/")).exists())
        self.assertEqual(external.read_bytes(), external_bytes)
        after = external.stat()
        self.assertEqual(after.st_size, before.st_size)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertEqual(after.st_dev, before.st_dev)

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
        for method_name in (
            "read_capture_json",
            "read_monitor_json",
            "read_settlement_json",
        ):
            method_signature = inspect.signature(
                getattr(runtime.V5RuntimeArtifactGateway, method_name)
            )
            for parameter_name in (
                "reference",
                "source_manifest",
                "expected_source_paths",
            ):
                self.assertEqual(
                    method_signature.parameters[parameter_name].default,
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
