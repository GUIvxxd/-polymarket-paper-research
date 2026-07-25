from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from typing import Callable
from unittest import mock

import xtracker_v5_isolation as isolation
import xtracker_v5_runtime_artifacts as runtime
import xtracker_v5_runtime_consumers as consumers


SOURCE_ROOT = Path(__file__).resolve().parent
CONSUMER_SOURCE = SOURCE_ROOT / "xtracker_v5_runtime_consumers.py"


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

    def consume(
        self,
        phase: str,
        references: tuple[isolation.ArtifactReference, ...],
        required_roles: tuple[str, ...],
        *,
        source_manifest: tuple[isolation.SourceDigest, ...] | None = None,
        expected_sources: tuple[str, ...] | None = None,
    ) -> tuple[runtime.VerifiedJSONArtifact, ...]:
        methods: dict[
            str,
            Callable[
                [
                    tuple[isolation.ArtifactReference, ...],
                    tuple[str, ...],
                    tuple[isolation.SourceDigest, ...],
                    tuple[str, ...],
                ],
                tuple[runtime.VerifiedJSONArtifact, ...],
            ],
        ] = {
            "capture": self.consumer.consume_capture,
            "monitor": self.consumer.consume_monitor,
            "settlement": self.consumer.consume_settlement,
        }
        return methods[phase](
            references,
            required_roles,
            self.source_manifest if source_manifest is None else source_manifest,
            self.expected_sources if expected_sources is None else expected_sources,
        )

    def test_successful_phase_batches_use_only_corresponding_gateway_methods(
        self,
    ) -> None:
        capture = self.make_reference(
            phase="capture",
            role="decision_book",
            data=b'{"phase":"capture","paper":true}\n',
            name="capture.json",
        )
        monitor = self.make_reference(
            phase="monitor",
            role="position_snapshot",
            data=b'{"phase":"monitor","paper":true}\n',
            name="monitor.json",
        )
        settlement = self.make_reference(
            phase="settlement",
            role="resolution_receipt",
            data=b'{"phase":"settlement","paper":true}\n',
            name="settlement.json",
        )

        capture_reader = runtime.V5RuntimeArtifactGateway.read_capture_json
        monitor_reader = runtime.V5RuntimeArtifactGateway.read_monitor_json
        settlement_reader = runtime.V5RuntimeArtifactGateway.read_settlement_json
        with (
            mock.patch.object(
                runtime.V5RuntimeArtifactGateway,
                "read_capture_json",
                autospec=True,
                side_effect=capture_reader,
            ) as capture_call,
            mock.patch.object(
                runtime.V5RuntimeArtifactGateway,
                "read_monitor_json",
                autospec=True,
                side_effect=monitor_reader,
            ) as monitor_call,
            mock.patch.object(
                runtime.V5RuntimeArtifactGateway,
                "read_settlement_json",
                autospec=True,
                side_effect=settlement_reader,
            ) as settlement_call,
        ):
            capture_result = self.consume(
                "capture",
                (capture,),
                ("decision_book",),
            )
            monitor_result = self.consume(
                "monitor",
                (monitor,),
                ("position_snapshot",),
            )
            settlement_result = self.consume(
                "settlement",
                (settlement,),
                ("resolution_receipt",),
            )

        self.assertEqual(capture_call.call_count, 1)
        self.assertEqual(monitor_call.call_count, 1)
        self.assertEqual(settlement_call.call_count, 1)
        for phase, reference, result in (
            ("capture", capture, capture_result),
            ("monitor", monitor, monitor_result),
            ("settlement", settlement, settlement_result),
        ):
            self.assertIsInstance(result, tuple)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].phase, phase)
            self.assertIs(result[0].reference, reference)
            self.assertTrue(result[0].parsed_object["paper"])

    def test_construction_requires_explicit_path_bundle_without_default(self) -> None:
        with self.assertRaises(TypeError):
            consumers.V5RuntimeArtifactConsumer()  # type: ignore[call-arg]
        with self.assertRaisesRegex(
            consumers.RuntimeConsumerContractError,
            "explicit V5PathBundle",
        ):
            consumers.V5RuntimeArtifactConsumer(self.root)  # type: ignore[arg-type]

        signature = inspect.signature(consumers.V5RuntimeArtifactConsumer)
        self.assertEqual(
            signature.parameters["paths"].default,
            inspect.Parameter.empty,
        )

    def test_manifest_and_expected_membership_are_explicit_and_mandatory(
        self,
    ) -> None:
        reference = self.make_reference(
            phase="capture",
            role="forecast_input",
            data=b'{"paper":true}\n',
            name="mandatory-source-contract.json",
        )
        signature = inspect.signature(
            consumers.V5RuntimeArtifactConsumer.consume_capture
        )
        for parameter in (
            "references",
            "required_roles",
            "source_manifest",
            "expected_source_paths",
        ):
            self.assertEqual(
                signature.parameters[parameter].default,
                inspect.Parameter.empty,
            )

        with self.assertRaises(TypeError):
            self.consumer.consume_capture(  # type: ignore[call-arg]
                (reference,),
                ("forecast_input",),
            )

        with mock.patch.object(runtime, "read_verified_artifact") as reader:
            with self.assertRaises(isolation.SourceIntegrityError):
                self.consumer.consume_capture(
                    (reference,),
                    ("forecast_input",),
                    (),
                    self.expected_sources,
                )
            with self.assertRaises(isolation.SourceIntegrityError):
                self.consumer.consume_capture(
                    (reference,),
                    ("forecast_input",),
                    self.source_manifest,
                    (),
                )
            reader.assert_not_called()

    def test_reference_objects_are_required_without_dict_conversion(self) -> None:
        with mock.patch.object(
            runtime.V5RuntimeArtifactGateway,
            "read_capture_json",
            autospec=True,
        ) as reader:
            with self.assertRaisesRegex(
                consumers.RuntimeConsumerContractError,
                "ArtifactReference",
            ):
                self.consumer.consume_capture(  # type: ignore[arg-type]
                    ({"role": "forecast_input"},),
                    ("forecast_input",),
                    self.source_manifest,
                    self.expected_sources,
                )
            reader.assert_not_called()

    def test_duplicate_missing_and_unexpected_roles_fail_before_gateway(self) -> None:
        forecast = self.make_reference(
            phase="capture",
            role="forecast_input",
            data=b'{"forecast":1}\n',
            name="forecast.json",
        )
        duplicate_forecast = self.make_reference(
            phase="capture",
            role="forecast_input",
            data=b'{"forecast":2}\n',
            name="forecast-duplicate.json",
        )
        fee = self.make_reference(
            phase="capture",
            role="fee_metadata",
            data=b'{"fee":1}\n',
            name="fee.json",
        )

        cases = (
            (
                "duplicate",
                (forecast, duplicate_forecast),
                ("forecast_input",),
                "duplicate requested role",
            ),
            (
                "missing",
                (forecast,),
                ("forecast_input", "fee_metadata"),
                "missing=",
            ),
            (
                "unexpected",
                (forecast, fee),
                ("forecast_input",),
                "unexpected=",
            ),
        )
        with mock.patch.object(
            runtime.V5RuntimeArtifactGateway,
            "read_capture_json",
            autospec=True,
        ) as reader:
            for label, references, roles, error in cases:
                with self.subTest(label=label):
                    with self.assertRaisesRegex(
                        consumers.RuntimeConsumerContractError,
                        error,
                    ):
                        self.consumer.consume_capture(
                            references,
                            roles,
                            self.source_manifest,
                            self.expected_sources,
                        )
            reader.assert_not_called()

    def test_mixed_valid_invalid_batch_exposes_no_partial_result(self) -> None:
        valid = self.make_reference(
            phase="capture",
            role="forecast_input",
            data=b'{"valid":true}\n',
            name="valid.json",
        )
        invalid = self.make_reference(
            phase="capture",
            role="fee_metadata",
            data=b'{"value":1}\n',
            name="invalid.json",
        )
        self.write_under_root(invalid.relative_path, b'{"value":2}\n')

        result: tuple[runtime.VerifiedJSONArtifact, ...] | None = None
        with self.assertRaisesRegex(isolation.ArtifactIntegrityError, "SHA-256"):
            result = self.consumer.consume_capture(
                (valid, invalid),
                ("forecast_input", "fee_metadata"),
                self.source_manifest,
                self.expected_sources,
            )
        self.assertIsNone(result)

    def test_source_manifest_failure_prevents_every_artifact_read(self) -> None:
        references = (
            self.make_reference(
                phase="monitor",
                role="monitor_book",
                data=b'{"book":{}}\n',
                name="book.json",
            ),
            self.make_reference(
                phase="monitor",
                role="position_snapshot",
                data=b'{"position":{}}\n',
                name="position.json",
            ),
        )
        duplicate_manifest = self.source_manifest + self.source_manifest
        with mock.patch.object(runtime, "read_verified_artifact") as reader:
            with self.assertRaisesRegex(
                isolation.SourceIntegrityError,
                "duplicate manifest",
            ):
                self.consumer.consume_monitor(
                    references,
                    ("monitor_book", "position_snapshot"),
                    duplicate_manifest,
                    self.expected_sources,
                )
            reader.assert_not_called()

    def test_replacing_pinned_source_after_manifest_construction_fails(self) -> None:
        reference = self.make_reference(
            phase="settlement",
            role="finalized_block",
            data=b'{"finalized":true}\n',
            name="source-replacement.json",
        )
        self.write_under_root(self.source_path, b"PAPER_ONLY = False\n")

        with mock.patch.object(runtime, "read_verified_artifact") as reader:
            with self.assertRaises(isolation.SourceIntegrityError):
                self.consumer.consume_settlement(
                    (reference,),
                    ("finalized_block",),
                    self.source_manifest,
                    self.expected_sources,
                )
            reader.assert_not_called()

    def test_artifact_hash_and_byte_length_mismatches_fail_closed(self) -> None:
        data = b'{"value":1}\n'
        valid = self.make_reference(
            phase="monitor",
            role="position_snapshot",
            data=data,
            name="integrity.json",
        )
        wrong_length = isolation.ArtifactReference(
            role=valid.role,
            relative_path=valid.relative_path,
            sha256=valid.sha256,
            byte_length=valid.byte_length + 1,
            source_type=valid.source_type,
        )
        wrong_hash = isolation.ArtifactReference(
            role=valid.role,
            relative_path=valid.relative_path,
            sha256="0" * 64,
            byte_length=valid.byte_length,
            source_type=valid.source_type,
        )

        for label, reference, error in (
            ("length", wrong_length, "byte length"),
            ("hash", wrong_hash, "SHA-256"),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    isolation.ArtifactIntegrityError,
                    error,
                ):
                    self.consumer.consume_monitor(
                        (reference,),
                        ("position_snapshot",),
                        self.source_manifest,
                        self.expected_sources,
                    )

    def test_wrong_phase_role_source_type_and_path_prefix_fail_closed(self) -> None:
        data = b'{"paper":true}\n'
        digest = hashlib.sha256(data).hexdigest()
        invalid_references = {
            "wrong-phase": isolation.ArtifactReference(
                role="forecast_input",
                relative_path=runtime.MONITOR_PATH_PREFIX + "wrong-phase.json",
                sha256=digest,
                byte_length=len(data),
                source_type="public_clob_rest",
            ),
            "wrong-role": isolation.ArtifactReference(
                role="resolution_receipt",
                relative_path=runtime.CAPTURE_PATH_PREFIX + "wrong-role.json",
                sha256=digest,
                byte_length=len(data),
                source_type="public_clob_rest",
            ),
            "wrong-source": isolation.ArtifactReference(
                role="forecast_input",
                relative_path=runtime.CAPTURE_PATH_PREFIX + "wrong-source.json",
                sha256=digest,
                byte_length=len(data),
                source_type="public_polygon_rpc",
            ),
            "wrong-prefix": isolation.ArtifactReference(
                role="forecast_input",
                relative_path="reports/not-v5/wrong-prefix.json",
                sha256=digest,
                byte_length=len(data),
                source_type="public_clob_rest",
            ),
        }

        with mock.patch.object(runtime, "read_verified_artifact") as reader:
            for label, reference in invalid_references.items():
                with self.subTest(label=label):
                    with self.assertRaises(runtime.RuntimeArtifactContractError):
                        self.consumer.consume_capture(
                            (reference,),
                            (reference.role,),
                            self.source_manifest,
                            self.expected_sources,
                        )
            reader.assert_not_called()

    def test_malformed_and_non_object_json_fail_closed(self) -> None:
        cases = (
            ("malformed", b'{"value":1,}\n'),
            ("non-object", b"[1,2,3]\n"),
        )
        for label, data in cases:
            with self.subTest(label=label):
                reference = self.make_reference(
                    phase="capture",
                    role="market_identity",
                    data=data,
                    name=f"{label}.json",
                )
                with self.assertRaises(runtime.RuntimeArtifactIntegrityError):
                    self.consume(
                        "capture",
                        (reference,),
                        ("market_identity",),
                    )

    def test_deep_valid_json_raises_integrity_error_not_recursion_error(self) -> None:
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
                self.consumer.consume_capture(
                    (reference,),
                    ("forecast_input",),
                    self.source_manifest,
                    self.expected_sources,
                )
        finally:
            sys.setrecursionlimit(original_limit)

        self.assertIsInstance(caught.exception.__cause__, RecursionError)

    def test_hostile_external_canaries_are_never_read_or_modified(self) -> None:
        cases = (
            (
                "capture",
                "decision_book",
                "public_clob_rest",
                b'{"origin":"temporary-capture"}\n',
                b'{"origin":"external-capture"}\n',
            ),
            (
                "monitor",
                "position_snapshot",
                "public_gamma_rest",
                b'{"origin":"temporary-monitor"}\n',
                b'{"origin":"external-monitor"}\n',
            ),
            (
                "settlement",
                "resolution_receipt",
                "public_polygon_rpc",
                b'{"origin":"temporary-settlement"}\n',
                b'{"origin":"external-settlement"}\n',
            ),
        )
        for phase, role, source_type, local_data, external_data in cases:
            with self.subTest(phase=phase):
                reference = self.make_reference(
                    phase=phase,
                    role=role,
                    data=local_data,
                    name=f"{phase}-canary.json",
                    source_type=source_type,
                )
                external = self.write_under_outside(
                    reference.relative_path,
                    external_data,
                )
                before_bytes = external.read_bytes()
                before_hash = hashlib.sha256(before_bytes).hexdigest()
                before_stat = external.stat()

                result = self.consume(phase, (reference,), (role,))

                self.assertEqual(result[0].raw_bytes, local_data)
                self.assertEqual(
                    result[0].parsed_object["origin"],
                    f"temporary-{phase}",
                )
                self.assertEqual(external.read_bytes(), before_bytes)
                self.assertEqual(
                    hashlib.sha256(external.read_bytes()).hexdigest(),
                    before_hash,
                )
                after_stat = external.stat()
                self.assertEqual(after_stat.st_size, before_stat.st_size)
                self.assertEqual(after_stat.st_mtime_ns, before_stat.st_mtime_ns)
                self.assertEqual(after_stat.st_ino, before_stat.st_ino)

    def test_intermediate_and_final_symlink_attacks_fail_closed(self) -> None:
        intermediate_external = self.outside / "intermediate"
        intermediate_external.mkdir()
        intermediate_data = b'{"external":"intermediate"}\n'
        (intermediate_external / "attack.json").write_bytes(intermediate_data)
        capture_parent = self.root.joinpath(
            *runtime.CAPTURE_PATH_PREFIX.rstrip("/").split("/")[:-1]
        )
        capture_parent.mkdir(parents=True)
        capture_link = capture_parent / "capture"
        try:
            capture_link.symlink_to(intermediate_external, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        intermediate_reference = isolation.ArtifactReference.from_bytes(
            role="decision_book",
            relative_path=runtime.CAPTURE_PATH_PREFIX + "attack.json",
            data=intermediate_data,
            source_type="public_clob_rest",
        )
        with self.assertRaises(isolation.RootIsolationError):
            self.consumer.consume_capture(
                (intermediate_reference,),
                ("decision_book",),
                self.source_manifest,
                self.expected_sources,
            )

        final_external = self.outside / "final-attack.json"
        final_data = b'{"external":"final"}\n'
        final_external.write_bytes(final_data)
        final_relative = runtime.MONITOR_PATH_PREFIX + "final-attack.json"
        final_local = self.root.joinpath(*final_relative.split("/"))
        final_local.parent.mkdir(parents=True)
        final_local.symlink_to(final_external)
        final_reference = isolation.ArtifactReference.from_bytes(
            role="monitor_book",
            relative_path=final_relative,
            data=final_data,
            source_type="public_gamma_rest",
        )
        with self.assertRaises(isolation.ArtifactIntegrityError):
            self.consumer.consume_monitor(
                (final_reference,),
                ("monitor_book",),
                self.source_manifest,
                self.expected_sources,
            )
        self.assertEqual(final_external.read_bytes(), final_data)

    def test_returned_results_are_deeply_immutable_and_retain_exact_bytes(
        self,
    ) -> None:
        data = b'{"nested":{"values":[1,{"two":2}]}}\n'
        reference = self.make_reference(
            phase="settlement",
            role="payout_state",
            data=data,
            name="immutable.json",
        )
        result = self.consume(
            "settlement",
            (reference,),
            ("payout_state",),
        )
        artifact = result[0]

        self.assertIsInstance(result, tuple)
        self.assertIsInstance(artifact.parsed_object, MappingProxyType)
        nested = artifact.parsed_object["nested"]
        self.assertIsInstance(nested, MappingProxyType)
        self.assertIsInstance(nested["values"], tuple)  # type: ignore[index]
        with self.assertRaises(TypeError):
            artifact.parsed_object["added"] = True  # type: ignore[index]
        with self.assertRaises(TypeError):
            nested["added"] = True  # type: ignore[index]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            artifact.phase = "capture"  # type: ignore[misc]
        self.assertIs(artifact.reference, reference)
        self.assertEqual(artifact.raw_bytes, data)
        self.assertEqual(artifact.sha256, hashlib.sha256(data).hexdigest())

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
