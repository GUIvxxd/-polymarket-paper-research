from __future__ import annotations

import csv
import hashlib
import json
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import verify_xtracker_forward_v5 as v5_verifier
import xtracker_forward_provenance as provenance
import xtracker_forward_engine_v5 as v5_engine
import xtracker_forward_v5 as v5


ROOT = Path(__file__).resolve().parent
ACTIVATION_TIME = datetime(2026, 7, 23, 14, 5, 6, 789000, tzinfo=UTC)
V4_LOCKED_HASHES = {
    "config/xtracker_forward_validation_v4.lock.json":
        "2001fac76d898babd06dd9da0264b480cfdd855a2bfd1997bf88d1c02ede2b55",
    "config/xtracker_forward_validation_v4.json":
        "2677b7507f34cec1779aaed9b8e66d69e713719771eea75dc02581f82b29c7a4",
    "xtracker_forward_capture.py":
        "7b73ab9c2714cf6dbe0f14dd756ffe9c2f115feba84dbc31a8cb03e461efa2ab",
    "xtracker_forward_engine_v4.py":
        "f8b897cad8abb7cd6e8cc8d3ea83440dcaf08bb3d559da4859b4d17e3e4efe8e",
    "xtracker_forward_monitor_v4.py":
        "0675976d22cd08948e746c458072cd58f442a2dec6b5da85447b84c94ae0e084",
    "xtracker_tweet_depth_check.py":
        "318075e9abbf79ecac854d16aa841e3f632f17e7485c0390cdf3f371e50ffa22",
    "xtracker_tweet_watchdog.py":
        "24635ed1cd97cae42a97cb5239a2e205182c6078e96122f243048e2761a1578a",
    "xtracker_paper_rebalance_ledger.py":
        "3acd8892d6c473028f781d4c89767aff4938b7f9a97611ac272ec7eeb374a4d2",
}


class V5ActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        required = set(v5.REQUIRED_SOURCE_PATHS) | {
            Path("config/xtracker_forward_validation_v4.json"),
            Path("config/xtracker_forward_validation_v4.lock.json"),
        }
        for relative_path in required:
            source = ROOT / relative_path
            target = self.root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        self.deployment_wrapper = self.root / "deployment/xtracker_tweet_watchdog_v5.sh"
        self.deployment_wrapper.parent.mkdir()
        self.deployment_wrapper.write_text(
            "#!/bin/sh\nexec python3 xtracker_forward_engine_v5.py\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def activate(self) -> dict:
        return v5.activate(
            self.root,
            confirmation=v5.ACTIVATION_CONFIRMATION,
            deployment_wrapper=self.deployment_wrapper,
            clock=lambda: ACTIVATION_TIME,
        )

    def test_activation_requires_explicit_confirmation(self) -> None:
        with self.assertRaisesRegex(v5.ActivationError, "confirmation"):
            v5.activate(self.root, confirmation="", clock=lambda: ACTIVATION_TIME)
        self.assertFalse(v5.paths_for(self.root).output.exists())

    def test_activation_requires_reviewed_deployment_wrapper_identity(self) -> None:
        with self.assertRaisesRegex(v5.ActivationError, "deployment wrapper"):
            v5.activate(
                self.root,
                confirmation=v5.ACTIVATION_CONFIRMATION,
                clock=lambda: ACTIVATION_TIME,
            )
        self.assertFalse(v5.paths_for(self.root).output.exists())

    def test_activation_creates_new_identity_and_fresh_timestamp(self) -> None:
        result = self.activate()
        paths = v5.paths_for(self.root)
        protocol = json.loads(paths.protocol.read_text(encoding="utf-8"))
        lock = json.loads(paths.lock.read_text(encoding="utf-8"))
        v4_lock = json.loads((self.root / "config/xtracker_forward_validation_v4.lock.json").read_text())

        self.assertTrue(result["protocol_id"].startswith("xtracker_forward_v5_20260723T140506789Z"))
        self.assertEqual(protocol["protocol_id"], result["protocol_id"])
        self.assertEqual(lock["activation_utc"], "2026-07-23T14:05:06.789Z")
        self.assertNotEqual(lock["activation_utc"], v4_lock["activation_utc"])
        self.assertEqual(
            lock["external_locked_source_sha256"],
            {str(self.deployment_wrapper.resolve()): hashlib.sha256(
                self.deployment_wrapper.read_bytes()
            ).hexdigest()},
        )
        self.assertEqual(provenance.verify_lock(self.root, lock), [])

    def test_initialization_is_zero_state_and_imports_no_v4_rows(self) -> None:
        v4_ledger = self.root / "reports/xtracker_forward_validation/v4/independent_event_ledger.csv"
        v4_ledger.parent.mkdir(parents=True)
        v4_ledger.write_text("baseline_lock_sha256\nforeign-v4-row\n", encoding="utf-8")
        before = v4_ledger.read_bytes()

        self.activate()
        paths = v5.paths_for(self.root)
        state = json.loads(paths.state.read_text(encoding="utf-8"))
        status = json.loads(paths.status.read_text(encoding="utf-8"))

        self.assertEqual(paths.registry.read_bytes(), b"")
        self.assertEqual(paths.events.read_bytes(), b"")
        with paths.ledger.open(newline="", encoding="utf-8") as handle:
            self.assertEqual(list(csv.DictReader(handle)), [])
        self.assertEqual(state["seen_opportunities"], {})
        self.assertTrue(all(not values for values in state["completed_clusters"].values()))
        self.assertEqual(status["registered_opportunities"], 0)
        self.assertTrue(all(value == 0 for value in status["executable_completed_clusters_by_arm"].values()))
        self.assertFalse(status["promotion_gate_passed"])
        self.assertEqual(v4_ledger.read_bytes(), before)

    def test_populated_target_fails_closed_before_writing_activation(self) -> None:
        paths = v5.paths_for(self.root)
        paths.output.mkdir(parents=True)
        marker = paths.output / "existing-evidence.jsonl"
        marker.write_text("preserve\n", encoding="utf-8")

        with self.assertRaisesRegex(v5.ActivationError, "not empty"):
            self.activate()

        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve\n")
        self.assertFalse(paths.protocol.exists())
        self.assertFalse(paths.lock.exists())

    def test_lock_source_byte_mismatch_fails_verification(self) -> None:
        self.activate()
        paths = v5.paths_for(self.root)
        lock = json.loads(paths.lock.read_text(encoding="utf-8"))
        engine = self.root / "xtracker_forward_engine_v5.py"
        engine.write_bytes(engine.read_bytes() + b"\n")

        errors = provenance.verify_lock(self.root, lock)

        self.assertTrue(any("locked_source_sha256" in error for error in errors), errors)

    def test_initial_verifier_passes_without_writing_audit(self) -> None:
        self.activate()
        paths = v5.paths_for(self.root)

        result = v5_verifier.audit(self.root, write_audit=False, verified_at="2026-07-23T14:06:00Z")

        self.assertTrue(result["ok"], result["errors"])
        self.assertFalse(paths.audit_json.exists())
        self.assertFalse(paths.audit_markdown.exists())

    def test_paper_only_zero_live_order_boundaries_are_initialized(self) -> None:
        self.activate()
        status = json.loads(v5.paths_for(self.root).status.read_text(encoding="utf-8"))
        self.assertTrue(status["paper_only"])
        self.assertEqual(status["live_orders"], 0)
        self.assertFalse(status["wallet_or_authentication_used"])
        self.assertTrue(status["aggregate_pnl_hidden_until_fixed_end"])
        self.assertNotIn("aggregate_net_pnl", status)
        self.assertNotIn("arm_ranking", status)

    def test_v5_economic_contract_matches_v4(self) -> None:
        self.activate()
        v4_protocol = json.loads((self.root / "config/xtracker_forward_validation_v4.json").read_text())
        v5_protocol = json.loads(v5.paths_for(self.root).protocol.read_text())
        for section in (
            "baseline",
            "registered_candidate_arms",
            "universe_and_independence",
            "causal_execution",
            "failure_accounting",
            "statistics_and_promotion",
        ):
            self.assertEqual(v5_protocol[section], v4_protocol[section], section)


class V5ProvenanceTests(unittest.TestCase):
    def test_new_v5_sources_contain_no_live_order_or_auth_path(self) -> None:
        source = "\n".join(
            (ROOT / relative_path).read_text(encoding="utf-8").lower()
            for relative_path in (
                "xtracker_forward_engine_v5.py",
                "xtracker_forward_provenance.py",
                "xtracker_forward_v5.py",
                "verify_xtracker_forward_v5.py",
            )
        )
        self.assertNotIn("/order", source)
        self.assertNotIn("private_key", source)
        self.assertNotIn("api_key", source)
        self.assertNotIn("authorization:", source)

    def test_append_only_chain_binds_identity_and_verifies(self) -> None:
        identity = {
            "protocol_id": "xtracker_forward_v5_test",
            "protocol_sha256": "a" * 64,
            "baseline_lock_sha256": "b" * 64,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            chain = {"sequence": 0, "last_hash": "0" * 64}
            for number in range(2):
                provenance.append_chain(
                    path,
                    provenance.bind_identity(
                        {"record_type": "SYNTHETIC", "number": number, "paper_only": True}, identity
                    ),
                    chain,
                )
            rows, head, errors = provenance.read_and_verify_chain(path, "events")

        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 2)
        self.assertEqual(head, chain["last_hash"])
        self.assertTrue(all(row["baseline_lock_sha256"] == "b" * 64 for row in rows))

    def test_foreign_and_missing_identity_records_fail(self) -> None:
        identity = {
            "protocol_id": "xtracker_forward_v5_test",
            "protocol_sha256": "a" * 64,
            "baseline_lock_sha256": "b" * 64,
        }
        foreign = provenance.bind_identity({"paper_only": True}, {**identity, "baseline_lock_sha256": "c" * 64})
        missing = provenance.bind_identity({"paper_only": True}, identity)
        del missing["protocol_id"]

        errors = provenance.verify_identity_rows([foreign, missing], identity, "records")

        self.assertTrue(any("baseline_lock_sha256:mismatch" in error for error in errors), errors)
        self.assertTrue(any("protocol_id:missing" in error for error in errors), errors)

    def test_existing_v4_locked_sources_remain_exact(self) -> None:
        for relative_path, expected in V4_LOCKED_HASHES.items():
            actual = hashlib.sha256((ROOT / relative_path).read_bytes()).hexdigest()
            self.assertEqual(actual, expected, relative_path)

    def test_runtime_adapter_binds_all_count_bearing_artifacts(self) -> None:
        lock = {
            "protocol_id": "xtracker_forward_v5_test",
            "protocol_sha256": "a" * 64,
            "lock_sha256": "b" * 64,
        }
        identity = provenance.active_identity(lock)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / "opportunity_registry.jsonl"
            events = root / "evidence_events.jsonl"
            ledger = root / "independent_event_ledger.csv"
            state_path = root / "state.json"
            status_path = root / "status.json"
            registry.write_bytes(b"")
            events.write_bytes(b"")
            with ledger.open("w", newline="", encoding="utf-8") as handle:
                csv.DictWriter(
                    handle, fieldnames=provenance.LEDGER_FIELDS, lineterminator="\n"
                ).writeheader()

            def append(path: Path, record: dict[str, Any], state: dict[str, Any], name: str) -> dict[str, Any]:
                chain = state.setdefault("chains", {}).setdefault(
                    name, {"sequence": 0, "last_hash": provenance.ZERO_HASH}
                )
                return provenance.append_chain(path, record, chain)

            def atomic_json(path: Path, value: Any) -> None:
                path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

            core = SimpleNamespace(
                REGISTRY=registry,
                EVENTS=events,
                LEDGER=ledger,
                STATE=state_path,
                STATUS=status_path,
                append_chain=append,
                atomic_json=atomic_json,
                ensure_ledger=lambda: None,
                sha_bytes=provenance.sha_bytes,
            )
            v5.install_runtime_provenance(core, lock)
            state = {
                "chains": {
                    "registry": {"sequence": 0, "last_hash": provenance.ZERO_HASH},
                    "events": {"sequence": 0, "last_hash": provenance.ZERO_HASH},
                },
                "seen_opportunities": {},
                "entered_lifecycles": {},
                "open_positions": {arm: {} for arm in provenance.ARMS},
                "completed_clusters": {arm: {} for arm in provenance.ARMS},
            }
            registered = core.append_chain(
                registry,
                {
                    "record_type": "OPPORTUNITY_REGISTERED",
                    "recorded_at": "2026-07-23T14:05:07Z",
                    "decision_time": "2026-07-23T14:05:07Z",
                    "opportunity_id": "opp-1",
                    "provisional_cluster_id": "cluster-1",
                    "lifecycle_id": "life-1",
                    "condition_id": "condition-1",
                    "yes_token_id": "token-1",
                    "event": "event-1",
                    "handle": "handle-1",
                    "bucket": "20-39",
                    "question": "question",
                    "selected_by_frozen_v3": True,
                    "watchdog_filter_version": "frozen",
                },
                state,
                "registry",
            )
            state["seen_opportunities"]["opp-1"] = {
                "lifecycle_id": "life-1", "condition_id": "condition-1"
            }
            core.add_ledger_row(registered, lock)
            entry = core.append_chain(
                events,
                {
                    "record_type": "ARM_ENTRY_EVALUATION",
                    "decision_time": "2026-07-23T14:05:07Z",
                    "opportunity_id": "opp-1",
                    "lifecycle_id": "life-1",
                    "arm": "baseline",
                    "decision": "PAPER_ENTRY",
                    "execution": {"complete": True},
                    "execution_evidence_eligible": True,
                },
                state,
                "events",
            )
            state["open_positions"]["baseline"]["life-1"] = {
                "opportunity_id": "opp-1",
                "entry_record_hash": entry["record_hash"],
            }
            core.atomic_json(state_path, state)
            bound_state = json.loads(state_path.read_text(encoding="utf-8"))
            status = {
                "paper_only": True,
                "live_orders": 0,
                "wallet_or_authentication_used": False,
                "registered_opportunities": 1,
                "executable_completed_clusters_by_arm": {arm: 0 for arm in provenance.ARMS},
                "net_capturable_completed_clusters_by_arm": {arm: 0 for arm in provenance.ARMS},
                "promotion_gate_passed": False,
                "aggregate_pnl_hidden_until_fixed_end": True,
                "chain_heads": bound_state["chains"],
            }
            core.atomic_json(status_path, status)
            bound_status = json.loads(status_path.read_text(encoding="utf-8"))
            registry_rows, _registry_head, registry_errors = provenance.read_and_verify_chain(
                registry, "registry"
            )
            event_rows, _event_head, event_errors = provenance.read_and_verify_chain(events, "events")
            with ledger.open(newline="", encoding="utf-8") as handle:
                ledger_rows = list(csv.DictReader(handle))

        errors = provenance.verify_experiment_provenance(
            lock, registry_rows, ledger_rows, event_rows, bound_state, bound_status
        )
        self.assertEqual(registry_errors + event_errors + errors, [])
        self.assertEqual(bound_state["baseline_lock_sha256"], identity["baseline_lock_sha256"])
        self.assertEqual(
            bound_state["open_positions"]["baseline"]["life-1"]["registry_record_hash"],
            registered["record_hash"],
        )

    def test_engine_sequence_preserves_one_exclusive_lock(self) -> None:
        calls: list[str] = []
        held = False

        class Core:
            @contextmanager
            def exclusive_run_lock(self, owner: str) -> Iterator[None]:
                nonlocal held
                self.assertEqual(owner, "xtracker_forward_engine_v5")
                held = True
                calls.append("lock-enter")
                try:
                    yield
                finally:
                    held = False
                    calls.append("lock-exit")

            @staticmethod
            def assertEqual(actual: str, expected: str) -> None:
                if actual != expected:
                    raise AssertionError((actual, expected))

            def main(self) -> int:
                self.assert_held()
                calls.append("capture")
                return 0

            @staticmethod
            def assert_held() -> None:
                if not held:
                    raise AssertionError("v5 sequence escaped its process lock")

        class Monitor:
            @staticmethod
            def main(_: Any) -> int:
                Core.assert_held()
                calls.append("monitor")
                return 0

        def enrich(_: Any) -> None:
            Core.assert_held()
            calls.append("enrich")

        result = v5_engine.run_locked_sequence(Core(), lambda: Monitor(), enrich=enrich)

        self.assertEqual(result, 0)
        self.assertEqual(calls, ["lock-enter", "capture", "enrich", "monitor", "lock-exit"])


if __name__ == "__main__":
    unittest.main()
