from __future__ import annotations

import hashlib
import importlib.util
import json
import unittest
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

import xtracker_forward_provenance as provenance
import verify_xtracker_forward_v4 as v4_verifier


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


ACTIVE_LOCK = {
    "protocol_id": "xtracker_forward_v4_20260720",
    "protocol_sha256": "2677b7507f34cec1779aaed9b8e66d69e713719771eea75dc02581f82b29c7a4",
    "lock_sha256": "6daadee95492e6b1cbedfe11af1f4ceaa209222ede21d858883d1e66c084442d",
    "activation_utc": "2026-07-20T15:50:54.234Z",
}
ORIGINAL_LOCK_SHA = "497bee78a3eeeb994747014299db2959d612a6a29f9473a59e6e714e4f438ef7"


def seal_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sealed = []
    previous_hash = "0" * 64
    for sequence, row in enumerate(rows, 1):
        body = {**row, "sequence": sequence, "previous_hash": previous_hash}
        record_hash = provenance.sha_bytes(previous_hash.encode() + provenance.canonical(body))
        sealed_row = {**body, "record_hash": record_hash}
        sealed.append(sealed_row)
        previous_hash = record_hash
    return sealed


def current_provenance_fixture() -> dict[str, Any]:
    identity = provenance.active_identity(ACTIVE_LOCK)
    registry = seal_rows([
        provenance.bind_identity(
            {
                "record_type": "OPPORTUNITY_REGISTERED",
                "opportunity_id": "opp-1",
                "lifecycle_id": "life-1",
                "decision_time": "2026-07-22T12:00:00Z",
                "paper_only": True,
                "live_order_submitted": False,
            },
            identity,
        )
    ])
    registry_hash = registry[0]["record_hash"]
    entry = provenance.bind_identity(
        {
            "record_type": "ARM_ENTRY_EVALUATION",
            "opportunity_id": "opp-1",
            "registry_record_hash": registry_hash,
            "lifecycle_id": "life-1",
            "arm": "baseline",
            "decision": "PAPER_ENTRY",
            "execution": {"complete": True},
            "execution_evidence_eligible": True,
            "paper_only": True,
            "live_order_submitted": False,
        },
        identity,
    )
    entry = seal_rows([entry])[0]
    completion = provenance.bind_identity(
        {
            "record_type": "ARM_SETTLEMENT",
            "opportunity_id": "opp-1",
            "registry_record_hash": registry_hash,
            "entry_record_hash": entry["record_hash"],
            "lifecycle_id": "life-1",
            "arm": "baseline",
            "execution_evidence_eligible": True,
            "paper_only": True,
            "live_order_submitted": False,
        },
        identity,
    )
    events = seal_rows([
        {key: value for key, value in entry.items() if key not in {"sequence", "previous_hash", "record_hash"}},
        completion,
    ])
    completion_hash = events[1]["record_hash"]
    completed_projection = provenance.bind_identity(
        {
            "opportunity_id": "opp-1",
            "registry_record_hash": registry_hash,
            "entry_record_hash": events[0]["record_hash"],
            "completion_record_hash": completion_hash,
        },
        identity,
    )
    state = provenance.bind_identity(
        {
            "seen_opportunities": {"opp-1": provenance.bind_identity(
                {"registry_record_hash": registry_hash}, identity
            )},
            "open_positions": {arm: {} for arm in provenance.ARMS},
            "completed_clusters": {
                **{arm: {} for arm in provenance.ARMS},
                "baseline": {"life-1": completed_projection},
            },
        },
        identity,
    )
    status = provenance.bind_identity(
        {
            "paper_only": True,
            "live_orders": 0,
            "wallet_or_authentication_used": False,
            "registered_opportunities": 1,
            "executable_completed_clusters_by_arm": {
                **{arm: 0 for arm in provenance.ARMS}, "baseline": 1,
            },
            "net_capturable_completed_clusters_by_arm": {
                **{arm: 0 for arm in provenance.ARMS}, "baseline": 1,
            },
            "promotion_gate_passed": False,
            "aggregate_pnl_hidden_until_fixed_end": True,
        },
        identity,
    )
    ledger = [{
        **identity,
        "experiment_identity_sha256": provenance.identity_binding_sha256(identity),
        "opportunity_id": "opp-1",
        "opportunity_sequence": "1",
        "registry_record_hash": registry_hash,
    }]
    return {
        "lock": dict(ACTIVE_LOCK),
        "registry": registry,
        "ledger": ledger,
        "events": events,
        "state": state,
        "status": status,
    }


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


class VerifierProvenanceTests(unittest.TestCase):
    def assert_has_error(self, errors: list[str], fragment: str) -> None:
        self.assertTrue(any(fragment in error for error in errors), errors)

    def test_confirmed_mixed_33_old_6_current_ledger_fails(self) -> None:
        identity = provenance.active_identity(ACTIVE_LOCK)
        rows = []
        for sequence in range(1, 40):
            row = {
                **identity,
                "baseline_lock_sha256": ORIGINAL_LOCK_SHA if sequence <= 33 else identity["baseline_lock_sha256"],
            }
            rows.append(row)

        errors = provenance.verify_identity_rows(rows, identity, "ledger")

        mismatches = [error for error in errors if "baseline_lock_sha256:mismatch" in error]
        self.assertEqual(len(mismatches), 33)

    def test_single_mismatched_or_missing_lock_field_fails(self) -> None:
        identity = provenance.active_identity(ACTIVE_LOCK)
        mismatched = {**identity, "baseline_lock_sha256": ORIGINAL_LOCK_SHA}
        missing = dict(identity)
        del missing["baseline_lock_sha256"]

        self.assert_has_error(
            provenance.verify_identity_rows([mismatched], identity, "ledger"),
            "baseline_lock_sha256:mismatch",
        )
        self.assert_has_error(
            provenance.verify_identity_rows([missing], identity, "ledger"),
            "baseline_lock_sha256:missing",
        )

    def test_mismatched_protocol_sha_or_id_fails(self) -> None:
        identity = provenance.active_identity(ACTIVE_LOCK)
        wrong_sha = {**identity, "protocol_sha256": "f" * 64}
        wrong_id = {**identity, "protocol_id": "xtracker_forward_v4_foreign"}

        self.assert_has_error(
            provenance.verify_identity_rows([wrong_sha], identity, "ledger"),
            "protocol_sha256:mismatch",
        )
        self.assert_has_error(
            provenance.verify_identity_rows([wrong_id], identity, "ledger"),
            "protocol_id:mismatch",
        )

    def test_all_current_identity_and_exact_linkage_pass(self) -> None:
        fixture = current_provenance_fixture()
        errors = v4_verifier.verify_provenance_records(**fixture)
        self.assertEqual(errors, [])

    def test_completion_from_foreign_lock_cannot_count(self) -> None:
        fixture = current_provenance_fixture()
        foreign_identity = {
            **provenance.active_identity(ACTIVE_LOCK),
            "baseline_lock_sha256": ORIGINAL_LOCK_SHA,
        }
        completion = fixture["events"][1]
        completion.update(foreign_identity)
        completion["experiment_identity_sha256"] = provenance.identity_binding_sha256(foreign_identity)

        errors = v4_verifier.verify_provenance_records(**fixture)

        self.assert_has_error(errors, "events[1]:baseline_lock_sha256:mismatch")

    def test_ambiguous_lifecycle_only_completion_fails_closed(self) -> None:
        fixture = current_provenance_fixture()
        fixture["events"][1].pop("entry_record_hash")
        fixture["state"]["completed_clusters"]["baseline"]["life-1"].pop("entry_record_hash")
        duplicate_entry = {
            key: value for key, value in fixture["events"][0].items()
            if key not in {"sequence", "previous_hash", "record_hash"}
        }
        fixture["events"] = seal_rows([duplicate_entry, duplicate_entry, {
            key: value for key, value in fixture["events"][1].items()
            if key not in {"sequence", "previous_hash", "record_hash"}
        }])

        errors = v4_verifier.verify_provenance_records(**fixture)

        self.assert_has_error(errors, "entry_record_hash:missing")

    def test_foreign_completed_state_projection_fails_closed(self) -> None:
        fixture = current_provenance_fixture()
        projection = fixture["state"]["completed_clusters"]["baseline"]["life-1"]
        projection["baseline_lock_sha256"] = ORIGINAL_LOCK_SHA

        errors = v4_verifier.verify_provenance_records(**fixture)

        self.assert_has_error(errors, "state.completed[baseline][life-1][0]:baseline_lock_sha256:mismatch")

    def test_pure_verification_does_not_write_audit_files(self) -> None:
        fixture = current_provenance_fixture()
        before = set(ROOT.glob("audit_latest.*"))

        result = v4_verifier.audit_records(**fixture, verified_at="2026-07-22T12:30:00Z")

        self.assertTrue(result["ok"])
        self.assertEqual(set(ROOT.glob("audit_latest.*")), before)


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
