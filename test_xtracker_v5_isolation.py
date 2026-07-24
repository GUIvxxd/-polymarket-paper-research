from __future__ import annotations

import hashlib
import inspect
import os
import tempfile
import unittest
from pathlib import Path

import xtracker_v5_isolation as isolation


SOURCE_ROOT = Path(__file__).resolve().parent
FOUNDATION_SOURCE = Path("xtracker_v5_isolation.py")
EXPECTED_FOUNDATION_SHA256 = "15a22b854242e8a6ac340cba8e9ea6e7c2f2780e429fafcb2e30bc01e9fe4329"


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
