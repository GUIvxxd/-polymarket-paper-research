from __future__ import annotations

import hashlib
import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import xtracker_v5_isolation as isolation


SOURCE_ROOT = Path(__file__).resolve().parent
FOUNDATION_SOURCE = Path("xtracker_v5_isolation.py")
EXPECTED_FOUNDATION_SHA256 = "0f6a8ba0d191874cce01694b0ed959e05389533cf289718eef93453fb9e0546c"


class V5IsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = tempfile.TemporaryDirectory()
        self.sandbox_root = Path(self.sandbox.name).resolve()
        self.root = self.sandbox_root / "explicit-v5-root"
        self.root.mkdir()
        self.outside = self.sandbox_root / "canonical-like-production"
        self.outside.mkdir()
        self.paths = isolation.V5PathBundle.from_root(self.root)

    def tearDown(self) -> None:
        self.sandbox.cleanup()

    def write_under_root(self, relative_path: str, data: bytes) -> Path:
        path = self.root.joinpath(*relative_path.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_path_bundle_requires_explicit_existing_absolute_root(self) -> None:
        with self.assertRaises(TypeError):
            isolation.V5PathBundle.from_root()  # type: ignore[call-arg]
        with self.assertRaisesRegex(isolation.RootIsolationError, "absolute"):
            isolation.V5PathBundle.from_root(Path("relative-root"))
        with self.assertRaisesRegex(isolation.RootIsolationError, "existing directory"):
            isolation.V5PathBundle.from_root(self.sandbox_root / "missing")

        for path in (
            self.paths.output,
            self.paths.raw,
            self.paths.proofs,
            self.paths.settlement_proofs,
            self.paths.state,
            self.paths.status,
            self.paths.registry,
            self.paths.events,
            self.paths.ledger,
            self.paths.audit,
        ):
            path.resolve().relative_to(self.root)
        self.assertFalse(self.paths.output.exists())

    def test_canary_external_canonical_like_proof_is_not_read_or_modified(self) -> None:
        external = (
            self.outside
            / "reports"
            / "xtracker_forward_validation"
            / "v5"
            / "settlement_proofs"
            / "condition-1.json"
        )
        external.parent.mkdir(parents=True)
        external_bytes = b'{"official_outcome":"external-only"}\n'
        external.write_bytes(external_bytes)
        before_stat = external.stat()
        reference = isolation.ArtifactReference.from_bytes(
            role="official-condition-proof",
            relative_path=(
                "reports/xtracker_forward_validation/v5/"
                "settlement_proofs/condition-1.json"
            ),
            data=external_bytes,
            source_type="sanitized-test-fixture",
        )

        with self.assertRaisesRegex(isolation.ArtifactIntegrityError, "missing"):
            isolation.read_verified_artifact(self.paths, reference)

        self.assertEqual(external.read_bytes(), external_bytes)
        after_stat = external.stat()
        self.assertEqual(after_stat.st_size, before_stat.st_size)
        self.assertEqual(after_stat.st_mtime_ns, before_stat.st_mtime_ns)
        self.assertFalse(
            self.paths.resolve_relative(reference.relative_path).exists()
        )

    def test_missing_altered_and_valid_local_proofs_are_distinct(self) -> None:
        data = b'{"condition_id":"condition-1","resolved":false}\n'
        reference = isolation.ArtifactReference.from_bytes(
            role="official-condition-proof",
            relative_path="reports/xtracker_forward_validation/v5/proofs/condition-1.json",
            data=data,
            source_type="sanitized-test-fixture",
        )
        with self.assertRaisesRegex(isolation.ArtifactIntegrityError, "missing"):
            isolation.read_verified_artifact(self.paths, reference)

        proof = self.write_under_root(reference.relative_path, data)
        self.assertEqual(isolation.read_verified_artifact(self.paths, reference), data)

        proof.write_bytes(b'{"condition_id":"condition-2","resolved":false}\n')
        with self.assertRaisesRegex(isolation.ArtifactIntegrityError, "SHA-256"):
            isolation.read_verified_artifact(self.paths, reference)

    def test_absolute_traversal_and_symlink_escape_are_rejected(self) -> None:
        external = self.outside / "proof.json"
        external.write_bytes(b"external\n")
        reference = isolation.ArtifactReference.from_bytes(
            role="proof",
            relative_path="proofs/proof.json",
            data=b"external\n",
            source_type="sanitized-test-fixture",
        )
        for unsafe in (str(external.resolve()), "../canonical-like-production/proof.json"):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(isolation.RootIsolationError):
                    isolation.read_verified_artifact(
                        self.paths,
                        isolation.ArtifactReference(
                            role=reference.role,
                            relative_path=unsafe,
                            sha256=reference.sha256,
                            byte_length=reference.byte_length,
                            source_type=reference.source_type,
                        ),
                    )

        link = self.root / "proofs"
        try:
            link.symlink_to(self.outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        with self.assertRaisesRegex(isolation.RootIsolationError, "symlink"):
            isolation.read_verified_artifact(self.paths, reference)
        self.assertEqual(external.read_bytes(), b"external\n")

    def test_root_replacement_after_bundle_validation_is_rejected(self) -> None:
        data = b'{"condition_id":"condition-root-race"}\n'
        relative_path = (
            "reports/xtracker_forward_validation/v5/proofs/condition-root-race.json"
        )
        reference = isolation.ArtifactReference.from_bytes(
            role="proof",
            relative_path=relative_path,
            data=data,
            source_type="sanitized-test-fixture",
        )
        self.write_under_root(relative_path, data)
        retired_root = self.sandbox_root / "retired-v5-root"
        self.root.rename(retired_root)
        self.root.mkdir()
        self.write_under_root(relative_path, data)

        with self.assertRaisesRegex(isolation.RootIsolationError, "identity changed"):
            isolation.read_verified_artifact(self.paths, reference)

        self.assertEqual(
            retired_root.joinpath(*relative_path.split("/")).read_bytes(),
            data,
        )

    def test_artifact_symlink_swap_during_descriptor_walk_is_rejected(self) -> None:
        data = b'{"condition_id":"condition-artifact-race"}\n'
        relative_path = (
            "reports/xtracker_forward_validation/v5/proofs/"
            "condition-artifact-race.json"
        )
        local = self.write_under_root(relative_path, data)
        external = self.outside / "condition-artifact-race.json"
        external.write_bytes(data)
        reference = isolation.ArtifactReference.from_bytes(
            role="proof",
            relative_path=relative_path,
            data=data,
            source_type="sanitized-test-fixture",
        )
        original_open = isolation._open_child_fd
        swapped = False

        def racing_open(name: str, flags: int, *, dir_fd: int) -> int:
            nonlocal swapped
            if name == local.name and not swapped:
                local.unlink()
                local.symlink_to(external)
                swapped = True
            return original_open(name, flags, dir_fd=dir_fd)

        with mock.patch.object(isolation, "_open_child_fd", side_effect=racing_open):
            with self.assertRaisesRegex(
                isolation.ArtifactIntegrityError,
                "safe root-local descriptor",
            ):
                isolation.read_verified_artifact(self.paths, reference)

        self.assertTrue(swapped)
        self.assertEqual(external.read_bytes(), data)

    def test_artifact_path_swap_after_open_reads_pinned_descriptor(self) -> None:
        local_data = b'{"proof":"local"}\n'
        external_data = b'{"proof":"other"}\n'
        relative_path = "proofs/after-open-race.json"
        local = self.write_under_root(relative_path, local_data)
        external = self.outside / "after-open-race.json"
        external.write_bytes(external_data)
        reference = isolation.ArtifactReference.from_bytes(
            role="proof",
            relative_path=relative_path,
            data=local_data,
            source_type="sanitized-test-fixture",
        )
        original_read = isolation.os.read
        swapped = False

        def racing_read(fd: int, byte_count: int) -> bytes:
            nonlocal swapped
            if not swapped:
                local.unlink()
                local.symlink_to(external)
                swapped = True
            return original_read(fd, byte_count)

        with mock.patch.object(isolation.os, "read", side_effect=racing_read):
            self.assertEqual(
                isolation.read_verified_artifact(self.paths, reference),
                local_data,
            )

        self.assertTrue(swapped)
        self.assertEqual(external.read_bytes(), external_data)

    def test_source_symlink_swap_during_descriptor_walk_is_rejected(self) -> None:
        data = b"VALUE = 1\n"
        relative_path = "src/future_v5_component.py"
        local = self.write_under_root(relative_path, data)
        external = self.outside / "future_v5_component.py"
        external.write_bytes(data)
        original_open = isolation._open_child_fd
        swapped = False

        def racing_open(name: str, flags: int, *, dir_fd: int) -> int:
            nonlocal swapped
            if name == local.name and not swapped:
                local.unlink()
                local.symlink_to(external)
                swapped = True
            return original_open(name, flags, dir_fd=dir_fd)

        with mock.patch.object(isolation, "_open_child_fd", side_effect=racing_open):
            with self.assertRaisesRegex(
                isolation.SourceIntegrityError,
                "safe root-local descriptor",
            ):
                isolation.build_source_manifest(self.paths, [Path(relative_path)])

        self.assertTrue(swapped)
        self.assertEqual(external.read_bytes(), data)

    def test_source_manifest_hashes_exact_canonical_lf_bytes(self) -> None:
        source = self.write_under_root("src/future_v5_component.py", b"VALUE = 1\n")
        manifest = isolation.build_source_manifest(
            self.paths, [Path("src/future_v5_component.py")]
        )

        self.assertEqual(
            manifest["src/future_v5_component.py"].sha256,
            hashlib.sha256(b"VALUE = 1\n").hexdigest(),
        )
        isolation.verify_source_manifest(self.paths, manifest)

        source.write_bytes(b"VALUE = 2\n")
        with self.assertRaisesRegex(isolation.SourceIntegrityError, "SHA-256"):
            isolation.verify_source_manifest(self.paths, manifest)

        source.write_bytes(b"VALUE = 1\r\n")
        with self.assertRaisesRegex(isolation.SourceIntegrityError, "canonical LF"):
            isolation.build_source_manifest(
                self.paths, [Path("src/future_v5_component.py")]
            )

    def test_missing_and_external_sources_fail_closed(self) -> None:
        with self.assertRaisesRegex(isolation.SourceIntegrityError, "missing"):
            isolation.build_source_manifest(self.paths, [Path("src/missing.py")])
        with self.assertRaises(isolation.RootIsolationError):
            isolation.build_source_manifest(self.paths, [self.outside / "external.py"])

    def test_checked_in_foundation_has_one_canonical_lf_digest(self) -> None:
        data = (SOURCE_ROOT / FOUNDATION_SOURCE).read_bytes()
        self.assertNotIn(b"\r", data)
        self.assertTrue(data.endswith(b"\n"))
        self.assertFalse(data.endswith(b"\n\n"))
        self.assertEqual(hashlib.sha256(data).hexdigest(), EXPECTED_FOUNDATION_SHA256)

    def test_foundation_has_no_production_root_or_unsafe_capability(self) -> None:
        source = (SOURCE_ROOT / FOUNDATION_SOURCE).read_text(encoding="utf-8").lower()
        self.assertNotIn("/data/workspace/polymarket-research", source)
        self.assertNotIn("requests", source)
        self.assertNotIn("subprocess", source)
        self.assertNotIn("private_key", source)
        self.assertNotIn("/order", source)
        signature = inspect.signature(isolation.V5PathBundle.from_root)
        self.assertEqual(signature.parameters["root"].default, inspect.Parameter.empty)

    def test_test_runtime_paths_are_confined_to_explicit_temporary_root(self) -> None:
        self.assertTrue(str(self.root).startswith(tempfile.gettempdir()))
        self.assertNotEqual(self.root, SOURCE_ROOT)
        self.assertNotIn("data/workspace/polymarket-research", self.root.as_posix())
        self.assertFalse(os.path.lexists(self.paths.output))


if __name__ == "__main__":
    unittest.main()
