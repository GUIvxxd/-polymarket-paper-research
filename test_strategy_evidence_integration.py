from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from datetime import UTC, datetime
from pathlib import Path

import event_ledger
import market_state_recorder
import multi_market_research_bot as mm
import stock_price_paper_bot as stockbot
import strategy_evidence
import xtracker_paper_rebalance_ledger as xledger


class StrategyEventTests(unittest.TestCase):
    def test_strategy_decision_normalizes_into_shared_event_ledger(self) -> None:
        signal = strategy_evidence.build_decision_signal(
            strategy="xtracker_rebalance",
            strategy_version="test-v1",
            action="ENTRY",
            lifecycle_id="life-1",
            position_id="7",
            decision_at="2026-07-18T00:00:05Z",
            decision_mode="decision_input_order_book",
            condition_id="0xabc",
            token_id="yes-token",
            outcome="Yes",
            quantity=100.0,
            question="Will X happen?",
            book={
                "request_started_at": "2026-07-18T00:00:01Z",
                "response_received_at": "2026-07-18T00:00:03Z",
                "bids": [{"price": "0.39", "size": "120"}],
                "asks": [{"price": "0.40", "size": "130"}],
            },
            eligible_markout_windows=["5m"],
        )
        event = event_ledger.normalize_signal(signal, ingested_at="2026-07-18T00:00:06Z")
        self.assertEqual(event["strategy_name"], "xtracker_rebalance")
        self.assertEqual(event["decision_action"], "ENTRY")
        self.assertEqual(event["market_lifecycle_id"], "life-1")
        self.assertEqual(event["decision_mode"], "decision_input_order_book")
        self.assertEqual(event["eligible_markout_windows"], ["5m"])
        self.assertEqual(event["targets"]["polymarket"][0]["token_id"], "yes-token")
        self.assertTrue(event["targets"]["polymarket"][0]["observe_exact_token"])

    def test_append_signals_is_idempotent(self) -> None:
        signal = strategy_evidence.build_decision_signal(
            strategy="stock_price_polymarket",
            strategy_version="test-v1",
            action="ENTRY",
            lifecycle_id="life-2",
            position_id="1",
            decision_at="2026-07-18T00:00:00Z",
            decision_mode="historical_replay_unverified",
            condition_id="0xdef",
            token_id=None,
            outcome="No",
            quantity=100.0,
            question="Will SPY close above 700?",
            eligible_markout_windows=[],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "signals.jsonl"
            first = strategy_evidence.append_signals(path, [signal])
            second = strategy_evidence.append_signals(path, [signal])
            self.assertEqual(first["appended"], 1)
            self.assertEqual(second["appended"], 0)
            self.assertEqual(len(list(event_ledger.read_jsonl(path))), 1)


class DecisionInputBookTests(unittest.TestCase):
    def test_book_received_before_decision_is_valid_input(self) -> None:
        state = market_state_recorder.polymarket_state_from_decision_book(
            {
                "bids": [{"price": "0.39", "size": "120"}],
                "asks": [{"price": "0.40", "size": "130"}],
            },
            token_id="yes-token",
            outcome="Yes",
            decision_at="2026-07-18T00:00:05Z",
            request_started_at="2026-07-18T00:00:01Z",
            response_received_at="2026-07-18T00:00:03Z",
            timing_quality="exact_request_response",
        )
        self.assertTrue(state["valid_baseline"])
        self.assertTrue(state["executable_quote"])
        self.assertEqual(state["snapshot_role"], "decision_input_order_book")
        self.assertEqual(state["decision_input_age_seconds"], 2.0)

    def test_legacy_snapshot_timestamp_cannot_be_promoted_to_valid_input(self) -> None:
        state = market_state_recorder.polymarket_state_from_decision_book(
            {
                "bids": [{"price": "0.39", "size": "120"}],
                "asks": [{"price": "0.40", "size": "130"}],
            },
            token_id="yes-token",
            outcome="Yes",
            decision_at="2026-07-18T00:00:05Z",
            request_started_at=None,
            response_received_at="2026-07-18T00:00:05Z",
            timing_quality="snapshot_timestamp_only",
        )
        self.assertFalse(state["valid_baseline"])
        self.assertFalse(state["execution_evidence_eligible"])


class StockDecisionEvidenceTests(unittest.TestCase):
    @patch("multi_market_research_bot.get_json")
    def test_top_book_preserves_full_depth_and_request_timing(self, get_json) -> None:
        get_json.return_value = {
            "timestamp": "1784332800000",
            "bids": [{"price": "0.39", "size": "120"}, {"price": "0.38", "size": "50"}],
            "asks": [{"price": "0.40", "size": "130"}, {"price": "0.41", "size": "60"}],
        }
        book = mm.top_book("token", "Yes")
        self.assertEqual(book.token_id, "token")
        self.assertEqual(len(book.bids or []), 2)
        self.assertEqual(len(book.asks or []), 2)
        self.assertIsNotNone(book.request_started_at)
        self.assertIsNotNone(book.response_received_at)
        self.assertIsNotNone(book.provider_timestamp)

    def test_stock_position_emits_entry_and_exit_into_shared_schema(self) -> None:
        position = {
            "id": 3, "market_key": "condition", "condition_id": "condition",
            "market_id": "10", "slug": "stock-market", "token_id": "yes-token",
            "question": "Will SPY close above 700?", "outcome": "Yes", "side": "YES",
            "ticker": "SPY", "stake_shares": 100.0,
            "entry_time": "2026-07-18T00:00:05Z", "entry_price": 0.40,
            "entry_bid": 0.39, "entry_bid_size": 120.0, "entry_ask_size": 130.0,
            "entry_book_bids": [(0.39, 120.0)], "entry_book_asks": [(0.40, 130.0)],
            "entry_book_request_started_at": "2026-07-18T00:00:01Z",
            "entry_book_response_received_at": "2026-07-18T00:00:03Z",
            "entry_book_timing_quality": "exact_request_response",
            "exit_time": "2026-07-18T01:00:05Z", "exit_price": 0.50,
            "exit_bid_size": 140.0, "exit_ask": 0.51, "exit_ask_size": 150.0,
            "exit_book_bids": [(0.50, 140.0)], "exit_book_asks": [(0.51, 150.0)],
            "exit_book_request_started_at": "2026-07-18T01:00:01Z",
            "exit_book_response_received_at": "2026-07-18T01:00:03Z",
            "exit_book_timing_quality": "exact_request_response",
        }
        signals = stockbot.strategy_signals_from_positions([position])
        self.assertEqual([row["decision_action"] for row in signals], ["ENTRY", "EXIT"])
        self.assertTrue(all(row["decision_mode"] == "decision_input_order_book" for row in signals))
        events = [event_ledger.normalize_signal(row) for row in signals]
        self.assertEqual(events[0]["market_lifecycle_id"], events[1]["market_lifecycle_id"])
        self.assertEqual(events[0]["targets"]["polymarket"][0]["token_id"], "yes-token")

    def test_stock_legacy_pnl_is_labeled_non_execution_valid(self) -> None:
        pos = {
            "entry_price": 0.40,
            "stake_shares": 100.0,
            "entry_book_timing_quality": "exact_request_response",
            "entry_ask_size": 150.0,
        }
        row = {
            "bid": 0.50,
            "bid_size": 150.0,
            "book_timing_quality": "exact_request_response",
        }
        closed = stockbot.close_position(pos, run_at="2026-07-18T01:00:00Z", exit_price=0.50, reason="test", row=row)
        summary = stockbot.summarize_positions([closed])
        self.assertTrue(closed["gross_top_of_book_feasible"])
        self.assertFalse(closed["execution_valid_pnl"])
        self.assertFalse(closed["net_capturable"])
        self.assertFalse(summary["execution_valid_pnl"])
        self.assertFalse(summary["net_capturable"])


class LifecycleAccountingTests(unittest.TestCase):
    def fixture(self) -> tuple[list[dict], list[xledger.ClosedTrade], list[xledger.Position]]:
        p1 = xledger.Position(
            position_id=1,
            event="Event A",
            handle="acct",
            bucket="0-9",
            question="Q1",
            entry_time="2026-07-18T00:00:00Z",
            entry_price=0.10,
            entry_fair=0.7,
            entry_edge=0.6,
            entry_count=1,
            entry_projected=5,
            yes_token_id="t1",
            condition_id="c1",
            source="initial",
            quantity=100.0,
        )
        t1 = xledger.close_position(
            p1,
            "2026-07-18T01:00:00Z",
            0.15,
            "profitable_better_bucket_available",
            None,
        )
        p2 = xledger.Position(
            position_id=2,
            event="Event A",
            handle="acct",
            bucket="10-19",
            question="Q2",
            entry_time="2026-07-18T01:00:00Z",
            entry_price=0.20,
            entry_fair=0.8,
            entry_edge=0.6,
            entry_count=5,
            entry_projected=15,
            yes_token_id="t2",
            condition_id="c2",
            source="rebalance",
            quantity=100.0,
        )
        t2 = xledger.close_position(p2, "settlement", 0.0, "settlement", None, final_count=30)
        ledger = [
            {"type": "ENTRY", **vars(p1)},
            {"type": "EXIT", **vars(t1)},
            {"type": "ENTRY", **vars(p2)},
            {"type": "SETTLEMENT", **vars(t2)},
        ]
        return ledger, [t1, t2], []

    def test_market_lifecycle_cash_invariant_and_switch_metrics(self) -> None:
        ledger, trades, open_positions = self.fixture()
        lifecycles = xledger.reconcile_lifecycles(ledger, trades, open_positions)
        self.assertEqual(len(lifecycles), 1)
        row = lifecycles[0]
        self.assertEqual(row["switches"], 1)
        self.assertEqual(row["closed_legs"], 2)
        self.assertFalse(row["execution_valid_pnl"])
        self.assertFalse(row["net_capturable"])
        self.assertAlmostEqual(row["fixed_100_share_pnl"], -15.0)
        self.assertAlmostEqual(
            row["ending_cash"] + row["inventory_value"] - row["initial_cash"],
            row["fixed_100_share_pnl"],
        )
        self.assertTrue(row["cash_invariant_reconciled"])

    def test_summary_prints_breakeven_and_payoff_metrics(self) -> None:
        ledger, trades, _ = self.fixture()
        zero = xledger.ClosedTrade(
            position_id=3,
            event="Event B",
            handle="acct",
            bucket="20-29",
            entry_time="2026-07-18T00:00:00Z",
            entry_price=0.05,
            exit_time="2026-07-18T00:10:00Z",
            exit_price=0.05,
            exit_reason="switch",
            pnl_per_share=0.0,
            roi_on_entry=0.0,
            paper_pnl=0.0,
            entry_count=1,
            exit_count=2,
            entry_fair=0.7,
            exit_fair=0.6,
            entry_edge=0.65,
            exit_edge=0.55,
            final_count=None,
            won_if_settled=None,
        )
        summary = xledger.summarize([*trades, zero])
        self.assertEqual(summary["closed_trades"], 3)
        self.assertEqual(summary["wins"] + summary["losses"] + summary["breakeven"], 3)
        self.assertFalse(summary["execution_valid_pnl"])
        self.assertFalse(summary["net_capturable"])
        self.assertIn("average_winner", summary)
        self.assertIn("average_loser", summary)
        self.assertIn("profit_factor", summary)
        self.assertIn("median_paper_pnl", summary)


if __name__ == "__main__":
    unittest.main()
