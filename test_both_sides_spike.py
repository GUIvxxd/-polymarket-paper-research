from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from hypothesis import given, settings, strategies as st

from both_sides_spike.book import BookReducer, PairState, walk_asks
from both_sides_spike.collector import Collector
from both_sides_spike.discovery import MarketValidationError, normalize_gamma_market, verify_clob_identity
from both_sides_spike.fees import FeeMetadata, calculate_fee, fee_gate
from both_sides_spike.pairing import account_partial_pair
from both_sides_spike.raw_log import DurableRawLog, verify_raw_log
from both_sides_spike.replay import replay_records

D = Decimal


def gamma_fixture(**overrides):
    row = {
        "id": "m1",
        "question": "Bitcoin Up or Down - July 18, 2:15PM-2:20PM ET",
        "slug": "btc-updown-5m-1784398500",
        "conditionId": "0xabc",
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": '["up-token", "down-token"]',
        "startDate": "2026-07-17T18:23:46Z",
        "eventStartTime": "2026-07-18T18:15:00Z",
        "endDate": "2026-07-18T18:20:00Z",
        "enableOrderBook": True,
        "acceptingOrders": True,
        "active": True,
        "closed": False,
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.07, "exponent": 1, "takerOnly": True},
    }
    row.update(overrides)
    return row


class DiscoveryTests(unittest.TestCase):
    def test_uses_event_start_and_up_down_identity(self):
        market = normalize_gamma_market(gamma_fixture(), verified_at="2026-07-18T18:14:00Z")
        self.assertEqual(market.asset, "BTC")
        self.assertEqual(market.duration_seconds, 300)
        self.assertEqual(market.up_token_id, "up-token")
        self.assertEqual(market.down_token_id, "down-token")
        self.assertEqual(market.published_at, "2026-07-17T18:23:46Z")
        self.assertEqual(market.publication_lead_ms, 85874000)

    def test_start_date_is_not_used_as_window_start(self):
        market = normalize_gamma_market(gamma_fixture())
        self.assertEqual(market.prediction_start, "2026-07-18T18:15:00Z")
        self.assertNotEqual(market.prediction_start, market.published_at)

    def test_rejects_wrong_duration_or_outcomes(self):
        with self.assertRaises(MarketValidationError):
            normalize_gamma_market(gamma_fixture(endDate="2026-07-18T18:21:00Z"))
        with self.assertRaises(MarketValidationError):
            normalize_gamma_market(gamma_fixture(outcomes='["Yes", "No"]'))

    def test_clob_cross_check_is_label_bound_not_position_only(self):
        market = normalize_gamma_market(gamma_fixture())
        payload = {
            "condition_id": "0xabc",
            "active": True,
            "closed": False,
            "minimum_tick_size": 0.01,
            "minimum_order_size": 5,
            "tokens": [
                {"outcome": "Down", "token_id": "down-token"},
                {"outcome": "Up", "token_id": "up-token"},
            ],
        }
        verified = verify_clob_identity(market, payload)
        self.assertEqual(verified.stage, "IDENTITY_VERIFIED")
        with self.assertRaises(MarketValidationError):
            verify_clob_identity(market, {**payload, "condition_id": "0xwrong"})


class FeeTests(unittest.TestCase):
    def test_separate_gamma_curve_and_token_consistency(self):
        meta = FeeMetadata.from_sources(
            fees_enabled=True,
            gamma_schedule={"rate": 0.07, "exponent": 1, "takerOnly": True},
            up_base_fee=1000,
            down_base_fee=1000,
        )
        gate = fee_gate(meta)
        self.assertTrue(gate.gamma_curve_supported)
        self.assertTrue(gate.token_order_fee_consistent)
        self.assertTrue(gate.economic_eligible)
        self.assertEqual(meta.gamma_rate, D("0.07"))
        self.assertEqual(meta.up_base_fee_bps, 1000)

    def test_documented_fee_examples_and_rounding(self):
        self.assertEqual(calculate_fee(D("100"), D("0.5"), D("0.07")).documented, D("1.75000"))
        self.assertEqual(calculate_fee(D("100"), D("0.1"), D("0.07")).documented, D("0.63000"))
        result = calculate_fee(D("0.001"), D("0.01"), D("0.07"))
        self.assertGreaterEqual(result.conservative, result.documented)

    @settings(max_examples=10_000, deadline=None)
    @given(
        q=st.decimals(min_value="0", max_value="10000", places=4, allow_nan=False, allow_infinity=False),
        p=st.decimals(min_value="0.001", max_value="0.999", places=3, allow_nan=False, allow_infinity=False),
    )
    def test_fee_symmetry_and_conservatism_property(self, q, p):
        left = calculate_fee(q, p, D("0.07"))
        right = calculate_fee(q, D("1") - p, D("0.07"))
        self.assertEqual(left.raw, right.raw)
        self.assertGreaterEqual(left.conservative, left.documented)


class BookTests(unittest.TestCase):
    def snapshot(self, token, bids=None, asks=None):
        return {
            "event_type": "book",
            "asset_id": token,
            "market": "0xabc",
            "bids": bids or [{"price": "0.48", "size": "10"}],
            "asks": asks or [{"price": "0.52", "size": "10"}],
            "timestamp": "1",
        }

    def test_snapshot_gate_gap_and_atomic_multitoken_update(self):
        reducer = BookReducer("0xabc", "up", "down", fresh_ms=1_000)
        change = {
            "event_type": "price_change",
            "market": "0xabc",
            "timestamp": "2",
            "price_changes": [
                {"asset_id": "up", "side": "SELL", "price": "0.51", "size": "8"},
                {"asset_id": "down", "side": "SELL", "price": "0.50", "size": "9"},
            ],
        }
        self.assertIsNone(reducer.apply(change, frame_index=1, received_monotonic_ns=1))
        reducer.apply(self.snapshot("up"), frame_index=2, received_monotonic_ns=10)
        pair = reducer.apply(self.snapshot("down"), frame_index=3, received_monotonic_ns=20)
        self.assertEqual(pair.state, PairState.PAIR_FRESH)
        atomic = reducer.apply(change, frame_index=4, received_monotonic_ns=30)
        self.assertEqual(atomic.state, PairState.PAIR_SAME_FRAME)
        self.assertEqual(atomic.up_best_ask, D("0.51"))
        self.assertEqual(atomic.down_best_ask, D("0.50"))
        reducer.open_gap("forced")
        self.assertFalse(reducer.pair_valid)
        self.assertIsNone(reducer.apply(change, frame_index=5, received_monotonic_ns=40))

    def test_old_unchanged_book_is_valid_but_not_fresh(self):
        reducer = BookReducer("0xabc", "up", "down", fresh_ms=100)
        reducer.apply(self.snapshot("up"), frame_index=1, received_monotonic_ns=1)
        pair = reducer.apply(self.snapshot("down"), frame_index=2, received_monotonic_ns=500_000_000)
        self.assertEqual(pair.state, PairState.PAIR_VALID)

    def test_observation_accounting_is_constant_memory(self):
        reducer = BookReducer("0xabc", "up", "down", fresh_ms=1_000)
        reducer.apply(self.snapshot("up"), frame_index=1, received_monotonic_ns=1)
        reducer.apply(self.snapshot("down"), frame_index=2, received_monotonic_ns=2)
        change = {
            "event_type": "price_change",
            "market": "0xabc",
            "timestamp": "2",
            "price_changes": [
                {"asset_id": "up", "side": "SELL", "price": "0.51", "size": "8"},
            ],
        }
        for frame_index in range(3, 100_003):
            reducer.apply(change, frame_index=frame_index, received_monotonic_ns=frame_index)

        self.assertEqual(reducer.observation_count, 100_001)
        self.assertEqual(reducer.latest_observation.frame_index, 100_002)
        self.assertFalse(hasattr(reducer, "observations"))

    @settings(max_examples=10_000, deadline=None)
    @given(
        sizes=st.lists(st.decimals(min_value="0.001", max_value="1000", places=3), min_size=1, max_size=8),
        request=st.decimals(min_value="0", max_value="3000", places=3),
    )
    def test_depth_walk_conservation_property(self, sizes, request):
        levels = [(D("0.1") + D(i) / D("100"), size) for i, size in enumerate(sizes)]
        fill = walk_asks(levels, request)
        self.assertLessEqual(fill.filled_quantity, request)
        self.assertLessEqual(fill.filled_quantity, sum(sizes, D("0")))
        self.assertEqual(fill.cost, sum((q * p for p, q in fill.levels), D("0")))


class PairingTests(unittest.TestCase):
    def test_partial_pair_separates_locked_and_directional_inventory(self):
        result = account_partial_pair(
            first_quantity=D("25"), first_cost=D("12.5"), first_fee=D("0.2"),
            second_quantity=D("15"), second_cost=D("7.2"), second_fee=D("0.1"),
        )
        self.assertEqual(result.matched_quantity, D("15"))
        self.assertEqual(result.unmatched_first_quantity, D("10"))
        self.assertEqual(result.pair_payout, D("15"))
        self.assertEqual(result.matched_quantity + result.unmatched_first_quantity, D("25"))


class RollingCollectorTests(unittest.TestCase):
    def test_desired_markets_include_active_and_near_future_only(self):
        now = datetime(2026, 7, 18, 18, 15, tzinfo=UTC)
        base = normalize_gamma_market(gamma_fixture())
        active = replace(
            base,
            condition_id="active",
            prediction_start=(now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            prediction_end=(now + timedelta(minutes=4)).isoformat().replace("+00:00", "Z"),
        )
        near = replace(
            base,
            condition_id="near",
            up_token_id="near-up",
            down_token_id="near-down",
            prediction_start=(now + timedelta(seconds=20)).isoformat().replace("+00:00", "Z"),
            prediction_end=(now + timedelta(minutes=5, seconds=20)).isoformat().replace("+00:00", "Z"),
        )
        far = replace(
            base,
            condition_id="far",
            up_token_id="far-up",
            down_token_id="far-down",
            prediction_start=(now + timedelta(seconds=31)).isoformat().replace("+00:00", "Z"),
            prediction_end=(now + timedelta(minutes=5, seconds=31)).isoformat().replace("+00:00", "Z"),
        )
        expired = replace(
            base,
            condition_id="expired",
            prediction_start=(now - timedelta(minutes=6)).isoformat().replace("+00:00", "Z"),
            prediction_end=now.isoformat().replace("+00:00", "Z"),
        )
        with tempfile.TemporaryDirectory() as td:
            collector = Collector(td, duration_seconds=10, rolling=True, prestart_lead_seconds=30)
            try:
                collector.registry = {m.condition_id: m for m in (active, near, far, expired)}
                desired = collector.desired_markets(now=now)
            finally:
                collector.close()
        self.assertEqual({m.condition_id for m in desired}, {"active", "near"})

    def test_subscription_fingerprint_changes_when_market_rotates(self):
        base = normalize_gamma_market(gamma_fixture())
        replacement = replace(base, condition_id="other", up_token_id="u2", down_token_id="d2")
        self.assertNotEqual(Collector.subscription_fingerprint([base]), Collector.subscription_fingerprint([replacement]))

    def test_disk_guard_requests_controlled_stop_below_floor(self):
        with tempfile.TemporaryDirectory() as td:
            collector = Collector(td, duration_seconds=10, rolling=True, minimum_free_bytes=1_000_000_000)
            try:
                collector._available_disk_bytes = lambda: 999_999_999
                self.assertFalse(collector.check_disk())
                self.assertTrue(collector.stop_requested)
                self.assertEqual(collector.stop_reason, "disk_floor_breached")
                self.assertEqual(collector.disk_samples[-1]["available_bytes"], 999_999_999)
            finally:
                collector.close()

    def test_signal_stop_request_is_flag_only_until_normal_loop_logs_it(self):
        with tempfile.TemporaryDirectory() as td:
            collector = Collector(td, duration_seconds=10, rolling=True)
            try:
                collector.log.append_frame(
                    b'{"event_type":"price_change","market":"0xabc","price_changes":[]}',
                    force_flush=False,
                )
                segments_before_signal = collector.log.segment_count
                collector.request_stop("signal_sigterm", append_event=False)
                self.assertTrue(collector.stop_requested)
                self.assertEqual(collector.stop_reason, "signal_sigterm")
                self.assertFalse(collector._stop_event_logged)
                self.assertEqual(collector.log.segment_count, segments_before_signal)

                collector._log_stop_requested_if_needed()
                self.assertTrue(collector._stop_event_logged)
                collector.collection_elapsed_seconds = 1.0
                manifest_path = collector.write_manifest()
            finally:
                collector.close()
            audit = verify_raw_log(json.loads(manifest_path.read_text())["raw_log"])
            self.assertTrue(audit.ok, audit.errors)

    def test_manifest_provenance_and_terminal_chain_hash_are_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            collector = Collector(td, duration_seconds=10, rolling=True)
            collector.collection_elapsed_seconds = 1.0
            manifest_path = collector.write_manifest()
            collector.close()
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest["terminal"]["final_chain_sha256"], collector.log.final_chain_hash)
            self.assertGreater(manifest["terminal"]["segment_count"], 0)
            self.assertIn("source_revision", manifest["provenance"])
            self.assertIn("starting_available_bytes", manifest["storage_guard"])
            self.assertEqual(manifest["configuration"]["minimum_free_bytes"], 1_000_000_000)
            audit = verify_raw_log(manifest["raw_log"])
            self.assertTrue(audit.ok, audit.errors)


class RawLogReplayTests(unittest.TestCase):
    def test_forced_flush_integrity_and_deterministic_replay(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "raw.jsonl"
            log = DurableRawLog(path, collector_run_id="run-1", connection_epoch=1)
            up = json.dumps({"event_type": "book", "asset_id": "up", "market": "0xabc", "bids": [], "asks": [{"price": "0.5", "size": "10"}]}).encode()
            down = json.dumps({"event_type": "book", "asset_id": "down", "market": "0xabc", "bids": [], "asks": [{"price": "0.49", "size": "10"}]}).encode()
            log.append_frame(up, force_flush=True)
            log.append_frame(down, force_flush=True)
            log.close()
            audit = verify_raw_log(path)
            self.assertTrue(audit.ok)
            self.assertEqual(audit.frame_count, 2)
            outputs = [replay_records(path, condition_id="0xabc", up_token_id="up", down_token_id="down") for _ in range(3)]
            self.assertEqual(outputs[0].canonical_hash, outputs[1].canonical_hash)
            self.assertEqual(outputs[1].canonical_hash, outputs[2].canonical_hash)

    def test_replay_accepts_polymarket_initial_snapshot_list_frame(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "raw.jsonl"
            payload = json.dumps([
                {"event_type": "book", "asset_id": "up", "market": "0xabc", "bids": [], "asks": [{"price": "0.51", "size": "10"}]},
                {"event_type": "book", "asset_id": "down", "market": "0xabc", "bids": [], "asks": [{"price": "0.48", "size": "10"}]},
            ]).encode()
            log = DurableRawLog(path, collector_run_id="run-list", connection_epoch=1)
            log.append_frame(payload, force_flush=True)
            log.close()
            result = replay_records(path, condition_id="0xabc", up_token_id="up", down_token_id="down")
            self.assertEqual(result.parsed_frames, 1)
            self.assertEqual(result.pair_observations, 1)

    def test_segmented_archive_is_durable_integral_and_replayable(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "raw_frames.bssraw"
            log = DurableRawLog(
                path,
                collector_run_id="run-segmented",
                connection_epoch=1,
                storage_format="segmented_v2",
                durability_window_ms=100,
            )
            up = json.dumps({"event_type": "book", "asset_id": "up", "market": "0xabc", "bids": [], "asks": [{"price": "0.5", "size": "10"}]}).encode()
            down = json.dumps({"event_type": "book", "asset_id": "down", "market": "0xabc", "bids": [], "asks": [{"price": "0.49", "size": "10"}]}).encode()
            log.append_frame(up, force_flush=False)
            log.append_frame(down, force_flush=False)
            self.assertEqual(log.durable_latency_count, 0)
            log.flush(force=True)
            self.assertEqual(log.durable_latency_count, 2)
            summary = log.durable_latency_summary()
            self.assertEqual(summary["count"], 2)
            self.assertIsNotNone(summary["p99_ms"])
            terminal = log.seal("unit_test_complete", {"result": "ok"})
            self.assertEqual(terminal["final_chain_sha256"], log.final_chain_hash)
            self.assertGreater(terminal["segment_count"], 0)
            log.close()

            audit = verify_raw_log(path)
            self.assertTrue(audit.ok, audit.errors)
            self.assertEqual(audit.frame_count, 2)
            outputs = [replay_records(path, condition_id="0xabc", up_token_id="up", down_token_id="down") for _ in range(3)]
            self.assertEqual(len({row.canonical_hash for row in outputs}), 1)
            self.assertEqual(outputs[0].pair_observations, 1)

    def test_zstd_segmented_archive_is_integral_and_replayable(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "raw_frames_zstd.bssraw"
            log = DurableRawLog(
                path,
                collector_run_id="run-zstd",
                connection_epoch=1,
                storage_format="segmented_v2",
                durability_window_ms=200,
                compression="zstd",
                compression_level=12,
            )
            up = json.dumps({"event_type": "book", "asset_id": "up", "market": "0xabc", "bids": [], "asks": [{"price": "0.5", "size": "10"}]}).encode()
            down = json.dumps({"event_type": "book", "asset_id": "down", "market": "0xabc", "bids": [], "asks": [{"price": "0.49", "size": "10"}]}).encode()
            log.append_frame(up, force_flush=False)
            log.append_frame(down, force_flush=False)
            log.seal("zstd_unit_test_complete")
            log.close()
            audit = verify_raw_log(path)
            self.assertTrue(audit.ok, audit.errors)
            self.assertEqual(audit.frame_count, 2)
            outputs = [replay_records(path, condition_id="0xabc", up_token_id="up", down_token_id="down") for _ in range(3)]
            self.assertEqual(len({row.canonical_hash for row in outputs}), 1)
            self.assertEqual(outputs[0].pair_observations, 1)

    def test_segmented_archive_detects_corruption_and_truncation(self):
        with tempfile.TemporaryDirectory() as td:
            original = Path(td) / "raw_frames.bssraw"
            log = DurableRawLog(original, collector_run_id="run-corruption", storage_format="segmented_v2")
            log.append_frame(b'{"event_type":"price_change","market":"0xabc","price_changes":[]}', force_flush=True)
            log.close()
            payload = original.read_bytes()

            corrupt = Path(td) / "corrupt.bssraw"
            changed = bytearray(payload)
            changed[-1] ^= 1
            corrupt.write_bytes(changed)
            corrupt_audit = verify_raw_log(corrupt)
            self.assertFalse(corrupt_audit.ok)
            self.assertTrue(any("hash" in error.lower() for error in corrupt_audit.errors), corrupt_audit.errors)

            truncated = Path(td) / "truncated.bssraw"
            truncated.write_bytes(payload[:-7])
            truncated_audit = verify_raw_log(truncated)
            self.assertFalse(truncated_audit.ok)
            self.assertTrue(any("truncat" in error.lower() for error in truncated_audit.errors), truncated_audit.errors)


if __name__ == "__main__":
    unittest.main()
