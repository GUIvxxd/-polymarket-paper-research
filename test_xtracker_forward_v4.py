from __future__ import annotations

import hashlib
import importlib.util
import json
import unittest
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parent


def load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256_exact(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def locked_source_in_checkout(raw_path: str) -> Path | None:
    source = PurePosixPath(raw_path)
    candidates = [ROOT.joinpath(*source.parts[-2:]), ROOT / source.name]
    return next((path for path in candidates if path.is_file()), None)


core = load(ROOT / "xtracker_forward_capture.py", "v4_core_test")
monitor = load(ROOT / "xtracker_forward_monitor_v4.py", "v4_monitor_test")
engine = load(ROOT / "xtracker_forward_engine_v4.py", "v4_engine_test")


class FrozenRuleTests(unittest.TestCase):
    def position(self) -> dict[str, Any]:
        return {"entry_vwap": 0.10, "event": "e", "bucket": "a"}

    def test_absolute_profit_exact_frozen_threshold(self) -> None:
        self.assertNotIn(
            "absolute_profit_exit",
            monitor.exit_reasons(
                core, "baseline", self.position(), {"complete": True, "vwap": 0.1299}, None, None
            ),
        )
        self.assertIn(
            "absolute_profit_exit",
            monitor.exit_reasons(
                core, "baseline", self.position(), {"complete": True, "vwap": 0.13}, None, None
            ),
        )

    def test_relative_profit_exact_frozen_threshold(self) -> None:
        self.assertNotIn(
            "relative_profit_exit",
            monitor.exit_reasons(
                core, "baseline", self.position(), {"complete": True, "vwap": 0.1199}, None, None
            ),
        )
        self.assertIn(
            "relative_profit_exit",
            monitor.exit_reasons(
                core, "baseline", self.position(), {"complete": True, "vwap": 0.1201}, None, None
            ),
        )

    def test_fair_collapse_and_stale_edge_exact(self) -> None:
        reasons = monitor.exit_reasons(
            core,
            "baseline",
            self.position(),
            {"complete": True, "vwap": 0.3001},
            {"fair": 0.20, "edge": 0.1},
            None,
        )
        self.assertIn("stale_bucket_bid_above_model", reasons)

    def test_better_bucket_delta_exact(self) -> None:
        reasons = monitor.exit_reasons(
            core,
            "baseline",
            self.position(),
            {"complete": True, "vwap": 0.10},
            {"fair": 0.5, "edge": 0.50},
            {"bucket": "b", "edge": 0.6001},
        )
        self.assertIn("profitable_better_bucket_available", reasons)

    def test_registered_drawdown_only_in_candidate_arm(self) -> None:
        walk = {"complete": True, "vwap": 0.075}
        self.assertNotIn(
            "registered_25pct_drawdown_exit",
            monitor.exit_reasons(core, "baseline", self.position(), walk, None, None),
        )
        self.assertIn(
            "registered_25pct_drawdown_exit",
            monitor.exit_reasons(core, "early_drawdown_exit_25pct", self.position(), walk, None, None),
        )


class SettlementTests(unittest.TestCase):
    def test_bucket_boundaries(self) -> None:
        self.assertTrue(monitor.bucket_hit("20-39", 20))
        self.assertTrue(monitor.bucket_hit("20-39", 39))
        self.assertFalse(monitor.bucket_hit("20-39", 40))
        self.assertTrue(monitor.bucket_hit("<20", 19))
        self.assertFalse(monitor.bucket_hit("<20", 20))
        self.assertTrue(monitor.bucket_hit("200+", 200))


class ProtocolTests(unittest.TestCase):
    def test_protocol_matches_frozen_constants(self) -> None:
        protocol = json.loads((ROOT / "config/xtracker_forward_validation_v4.json").read_text())
        rules = protocol["baseline"]["exit_rules_exactly_from_frozen_source"]
        self.assertEqual(rules["minimum_absolute_profit_per_share"], 0.03)
        self.assertEqual(rules["minimum_relative_profit"], 0.20)
        self.assertEqual(rules["better_bucket_edge_delta"], 0.10)
        self.assertEqual(rules["rebalance_minimum_edge"], 0.50)
        self.assertEqual(rules["rebalance_minimum_fair"], 0.70)
        self.assertEqual(rules["rebalance_maximum_ask"], 0.25)

    def test_v3_is_explicitly_excluded(self) -> None:
        protocol = json.loads((ROOT / "config/xtracker_forward_validation_v4.json").read_text())
        self.assertIn("v3_pilot", protocol["excluded_samples"])

    def test_lock_self_hash_and_locked_repo_sources_match_exact_bytes(self) -> None:
        lock = json.loads((ROOT / "config/xtracker_forward_validation_v4.lock.json").read_text())
        body = {key: value for key, value in lock.items() if key != "lock_sha256"}
        expected = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        self.assertEqual(lock["lock_sha256"], expected)
        for raw_path, digest in lock["locked_source_sha256"].items():
            local_path = locked_source_in_checkout(raw_path)
            if local_path is None:
                continue
            self.assertEqual(sha256_exact(local_path), digest, str(local_path.relative_to(ROOT)))

    def test_unchanged_protocol_sources_retain_registered_hashes(self) -> None:
        lock = json.loads((ROOT / "config/xtracker_forward_validation_v4.lock.json").read_text())
        expected = {
            "config/xtracker_forward_validation_v4.json":
                "2677b7507f34cec1779aaed9b8e66d69e713719771eea75dc02581f82b29c7a4",
            "xtracker_forward_monitor_v4.py":
                "0675976d22cd08948e746c458072cd58f442a2dec6b5da85447b84c94ae0e084",
            "xtracker_tweet_depth_check.py":
                "318075e9abbf79ecac854d16aa841e3f632f17e7485c0390cdf3f371e50ffa22",
            "xtracker_tweet_watchdog.py":
                "24635ed1cd97cae42a97cb5239a2e205182c6078e96122f243048e2761a1578a",
        }
        registered = {
            str(local.relative_to(ROOT)).replace("\\", "/"): digest
            for raw_path, digest in lock["locked_source_sha256"].items()
            if (local := locked_source_in_checkout(raw_path)) is not None
        }
        for path, digest in expected.items():
            self.assertEqual(registered[path], digest, path)


class SafetyTests(unittest.TestCase):
    def test_locked_sources_have_no_order_or_auth_api(self) -> None:
        text = "\n".join(
            (ROOT / name).read_text().lower()
            for name in (
                "xtracker_forward_capture.py",
                "xtracker_forward_monitor_v4.py",
                "xtracker_forward_engine_v4.py",
            )
        )
        self.assertNotIn("/order", text)
        self.assertNotIn("private_key", text)
        self.assertNotIn("api_key", text)
        self.assertNotIn("authorization", text)

    def test_contended_engine_runs_no_capture_enrich_or_monitor(self) -> None:
        calls: list[str] = []

        class ContendedCore:
            @contextmanager
            def exclusive_run_lock(self, owner: str) -> Iterator[None]:
                self.assert_owner(owner)
                raise SystemExit("exclusive run lock already held")
                yield

            @staticmethod
            def assert_owner(owner: str) -> None:
                if owner != "xtracker_forward_engine_v4":
                    raise AssertionError(owner)

            def main(self) -> int:
                calls.append("capture")
                return 0

        def enrich(_: Any) -> None:
            calls.append("enrich")

        def load_monitor() -> Any:
            calls.append("load-monitor")
            return object()

        with self.assertRaisesRegex(SystemExit, "already held"):
            engine.run_locked_sequence(ContendedCore(), load_monitor, enrich=enrich)
        self.assertEqual(calls, [])

    def test_capture_enrich_monitor_run_in_order_under_one_lock(self) -> None:
        calls: list[str] = []
        held = False

        class FakeCore:
            @contextmanager
            def exclusive_run_lock(self, owner: str) -> Iterator[None]:
                nonlocal held
                self.assert_owner(owner)
                calls.append("lock-enter")
                held = True
                try:
                    yield
                finally:
                    held = False
                    calls.append("lock-exit")

            @staticmethod
            def assert_owner(owner: str) -> None:
                if owner != "xtracker_forward_engine_v4":
                    raise AssertionError(owner)

            def main(self) -> int:
                self.assert_locked()
                calls.append("capture")
                return 0

            @staticmethod
            def assert_locked() -> None:
                if not held:
                    raise AssertionError("sequence ran outside lock")

        class FakeMonitor:
            @staticmethod
            def main(_: Any) -> int:
                FakeCore.assert_locked()
                calls.append("monitor")
                return 7

        def enrich(_: Any) -> None:
            FakeCore.assert_locked()
            calls.append("enrich")

        def load_monitor() -> FakeMonitor:
            FakeCore.assert_locked()
            calls.append("load-monitor")
            return FakeMonitor()

        result = engine.run_locked_sequence(FakeCore(), load_monitor, enrich=enrich)
        self.assertEqual(result, 7)
        self.assertEqual(
            calls,
            ["lock-enter", "capture", "enrich", "load-monitor", "monitor", "lock-exit"],
        )


if __name__ == "__main__":
    unittest.main()
