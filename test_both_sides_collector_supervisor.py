from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path("/data/scripts/both_sides_collector_supervisor.py")
spec = importlib.util.spec_from_file_location("both_sides_collector_supervisor", MODULE_PATH)
assert spec and spec.loader
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)


class StoragePreflightTests(unittest.TestCase):
    def test_rejects_observed_host_capacity_for_24_hours(self):
        result = supervisor.storage_preflight(86_400, free_bytes=3_321_925_632)
        self.assertFalse(result["accepted"])
        self.assertGreater(result["shortfall_bytes"], 0)
        self.assertGreaterEqual(result["minimum_free_bytes"], 1_000_000_000)

    def test_accepts_two_hour_qualification_at_same_capacity(self):
        result = supervisor.storage_preflight(7_200, free_bytes=3_321_925_632)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["shortfall_bytes"], 0)
        self.assertGreater(result["projected_archive_bytes_with_headroom"], result["projected_archive_bytes"])

    def test_never_counts_disk_floor_as_writable_capacity(self):
        result = supervisor.storage_preflight(7_200, free_bytes=1_000_000_000)
        self.assertFalse(result["accepted"])
        self.assertEqual(
            result["required_starting_available_bytes"],
            result["minimum_free_bytes"] + result["projected_archive_bytes_with_headroom"],
        )


class ProcessIdentityTests(unittest.TestCase):
    def test_pid_existence_without_exact_command_is_rejected(self):
        identity = {"boot_id": "boot", "start_ticks": 123}
        state = {"pid": 42, "run_dir": "/tmp/pinned-run", "process_identity": identity}
        with mock.patch.object(supervisor.Path, "exists", return_value=True), mock.patch.object(
            supervisor, "proc_cmdline", return_value=["python", "unrelated.py"]
        ), mock.patch.object(supervisor, "process_identity", return_value=identity):
            self.assertFalse(supervisor.process_matches(state))

    def test_exact_runner_and_run_directory_are_required(self):
        identity = {"boot_id": "boot", "start_ticks": 123}
        state = {"pid": 42, "run_dir": "/tmp/pinned-run", "process_identity": identity}
        cmd = ["python", str(supervisor.RUNNER), "--run-dir", "/tmp/pinned-run"]
        with mock.patch.object(supervisor.Path, "exists", return_value=True), mock.patch.object(
            supervisor, "proc_cmdline", return_value=cmd
        ), mock.patch.object(supervisor, "process_identity", return_value=identity):
            self.assertTrue(supervisor.process_matches(state))

    def test_reused_pid_with_different_start_time_is_rejected(self):
        state = {
            "pid": 42,
            "run_dir": "/tmp/pinned-run",
            "process_identity": {"boot_id": "boot", "start_ticks": 123},
        }
        with mock.patch.object(supervisor.Path, "exists", return_value=True), mock.patch.object(
            supervisor, "process_identity", return_value={"boot_id": "boot", "start_ticks": 999}
        ):
            self.assertFalse(supervisor.process_matches(state))


class PersistenceTests(unittest.TestCase):
    def test_atomic_json_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            supervisor.atomic_json(path, {"paper_only": True, "live_orders_enabled": False})
            self.assertEqual(json.loads(path.read_text()), {"paper_only": True, "live_orders_enabled": False})
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_strict_json_does_not_turn_corruption_into_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                supervisor.load_json_strict(path)


class ManifestValidationTests(unittest.TestCase):
    def valid_manifest(self):
        return {
            "schema_version": "both_sides_rolling_manifest_v1",
            "collection_mode": "rolling",
            "collector_run_id": "run-1",
            "raw_log": "/tmp/run/raw_frames.bssraw",
            "duration_requested_seconds": 7200,
            "collection_elapsed_seconds": 7200,
            "terminal_reason": "collector_deadline",
            "controlled_stop": False,
            "terminal": {"final_chain_sha256": "abc"},
            "configuration": {"paper_only": True, "live_orders_enabled": False},
            "provenance": {"source_revision": {"kind": "source_tree_sha256", "value": "rev", "files": []}},
            "storage_guard": {"minimum_free_bytes": 1_000_000_000, "ending_available_bytes": 2_000_000_000},
            "required_market_classes": sorted(supervisor.REQUIRED_MARKET_CLASSES),
            "parser_errors": 0,
            "live_orders": 0,
            "wallet_or_authentication_used": False,
        }

    def state(self):
        return {
            "raw_path": "/tmp/run/raw_frames.bssraw",
            "duration_seconds": 7200,
            "collector_run_id": "run-1",
            "collector_source_revision_expected": {"kind": "source_tree_sha256", "value": "rev", "files": []},
        }

    def test_valid_deadline_manifest_passes(self):
        self.assertEqual(supervisor.validate_manifest(self.valid_manifest(), self.state()), [])

    def test_manifest_requires_paper_only_chain_and_duration(self):
        manifest = self.valid_manifest()
        manifest["live_orders"] = 1
        manifest["terminal"] = {}
        manifest["collection_elapsed_seconds"] = 10
        errors = supervisor.validate_manifest(manifest, self.state())
        self.assertTrue(any("paper-only" in error for error in errors))
        self.assertTrue(any("chain hash" in error for error in errors))
        self.assertTrue(any("too short" in error for error in errors))

    def test_normal_transient_states_are_allowed(self):
        self.assertIn("WAITING_FOR_MARKETS", supervisor.HEALTHY_COLLECTOR_STATES)
        self.assertIn("RECONNECTING", supervisor.HEALTHY_COLLECTOR_STATES)

    def test_problem_tick_returns_scheduler_failure(self):
        self.assertEqual(supervisor.tick_exit_code({"state": "STOP_REQUESTED", "problems": ["x"]}), 3)

    def test_terminal_tick_clears_stale_alive_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_path = root / "raw_frames.bssraw"
            raw_path.write_bytes(b"sealed")
            manifest_path = root / "manifest.json"
            status_path = root / "status.json"
            manifest = self.valid_manifest()
            manifest["raw_log"] = str(raw_path)
            manifest["durable_frame_count"] = 7_932_833
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            status_path.write_text(json.dumps({
                "state": "COMPLETE",
                "heartbeat_at": "2026-07-20T16:39:35Z",
                "durable_frame_count": 7_932_833,
            }), encoding="utf-8")
            state = self.state()
            state.update({
                "pid": 42,
                "run_dir": str(root),
                "manifest_path": str(manifest_path),
                "status_path": str(status_path),
                "raw_path": str(raw_path),
                "process_identity": {"boot_id": "boot", "start_ticks": 123},
                "collector_alive": True,
            })
            with mock.patch.object(supervisor, "STATE_PATH", root / "state.json"), mock.patch.object(
                supervisor, "HEARTBEAT_PATH", root / "heartbeat.json"
            ), mock.patch.object(supervisor, "process_matches", return_value=False), mock.patch.object(
                supervisor, "emit_once"
            ):
                result = supervisor.tick_locked(state)
            self.assertEqual(result["state"], "COMPLETE")
            self.assertFalse(result["collector_alive"])
            self.assertEqual(result["collector_status_state"], "COMPLETE")
            self.assertEqual(result["last_frames"], 7_932_833)
            self.assertIsNone(result["last_rss_bytes"])


if __name__ == "__main__":
    unittest.main()
