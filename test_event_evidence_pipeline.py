#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import event_ledger
import evidence_report
import market_state_recorder
import markout_worker
import public_record_reaction_bot


WATCHDOG_TEMPLATE = Path(__file__).resolve().parent / "deploy" / "scripts" / "event_evidence_pipeline_watchdog.sh"
FAKE_CONTROL_ENVIRONMENT = (
    "FAILURE_COMMAND",
    "FAILURE_RC",
    "MARKOUT_RC",
    "REPORT_RC",
    "PIPELINE_CALL_LOG",
    "EVENT_EVIDENCE_PROJECT_ROOT",
    "PIPELINE_FAKE_REPORT_PATH",
)


@unittest.skipIf(os.name == "nt", "pipeline wrapper tests require POSIX bash and flock")
@unittest.skipUnless(shutil.which("bash") and shutil.which("flock"), "bash and flock are required")
class EventEvidencePipelineWrapperTests(unittest.TestCase):
    def run_pipeline(
        self,
        *,
        markout_rc: int | None = None,
        report_rc: int | None = None,
        failure_command: str | None = None,
        failure_rc: int | None = None,
        source_files: dict[str, str] | None = None,
        hold_lock: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], list[str], bool, bool]:
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            (project_root / "reports").mkdir()
            report_path = project_root / "reports" / "event_evidence_report_latest.json"
            report_path.write_text("stale\n", encoding="utf-8")
            wrapper = project_root / "event_evidence_pipeline_watchdog.sh"
            wrapper.write_text(WATCHDOG_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
            for relative_path, content in (source_files or {}).items():
                source_path = project_root / relative_path
                source_path.parent.mkdir(parents=True, exist_ok=True)
                source_path.write_text(content, encoding="utf-8")
            bin_dir = project_root / "bin"
            bin_dir.mkdir()
            fake_python = bin_dir / "python3"
            fake_python.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$PIPELINE_CALL_LOG\"\n"
                "if [ \"$*\" = \"${FAILURE_COMMAND:-__no_failure__}\" ]; then\n"
                "  exit \"${FAILURE_RC:-1}\"\n"
                "fi\n"
                "if [ \"$1\" = markout_worker.py ]; then exit \"${MARKOUT_RC:-0}\"; fi\n"
                "if [ \"$1\" = evidence_report.py ]; then\n"
                "  printf 'refreshed\\n' > \"$PIPELINE_FAKE_REPORT_PATH\"\n"
                "  exit \"${REPORT_RC:-0}\"\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            call_log = project_root / "calls.log"
            env = dict(os.environ)
            for key in FAKE_CONTROL_ENVIRONMENT:
                env.pop(key, None)
            env.update({
                "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
                "PIPELINE_CALL_LOG": str(call_log),
                "EVENT_EVIDENCE_PROJECT_ROOT": str(project_root),
                "PIPELINE_FAKE_REPORT_PATH": str(report_path),
            })
            if markout_rc is not None:
                env["MARKOUT_RC"] = str(markout_rc)
            if report_rc is not None:
                env["REPORT_RC"] = str(report_rc)
            if failure_command is not None:
                env["FAILURE_COMMAND"] = failure_command
            if failure_rc is not None:
                env["FAILURE_RC"] = str(failure_rc)

            holder: subprocess.Popen[str] | None = None
            if hold_lock:
                lock_path = project_root / "reports" / ".event_evidence_pipeline.lock"
                holder = subprocess.Popen(
                    [
                        "bash",
                        "-c",
                        'exec 8>"$1"; flock -n 8 || exit 99; printf "ready\\n"; read -r _',
                        "lock-holder",
                        str(lock_path),
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self.assertIsNotNone(holder.stdout)
                self.assertEqual(holder.stdout.readline(), "ready\n")

            try:
                result = subprocess.run(
                    ["bash", str(wrapper)],
                    cwd=project_root,
                    env=env,
                    check=False,
                    capture_output=True,
                    text=True,
                )
            finally:
                if holder is not None:
                    self.assertIsNotNone(holder.stdin)
                    holder.stdin.write("release\n")
                    holder.stdin.close()
                    holder.wait(timeout=5)
                    self.assertIsNotNone(holder.stdout)
                    self.assertIsNotNone(holder.stderr)
                    holder.stdout.close()
                    holder.stderr.close()

            calls = call_log.read_text(encoding="utf-8").splitlines() if call_log.exists() else []
            lock_exists = (project_root / "reports" / ".event_evidence_pipeline.lock").exists()
            report_refreshed = report_path.read_text(encoding="utf-8") == "refreshed\n"
            return result, calls, lock_exists, report_refreshed

    def test_clean_markout_refreshes_report_and_returns_zero(self) -> None:
        result, calls, lock_exists, report_refreshed = self.run_pipeline()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            calls,
            ["event_ledger.py", "market_state_recorder.py", "markout_worker.py", "evidence_report.py"],
        )
        self.assertTrue(lock_exists)
        self.assertTrue(report_refreshed)

    def test_hostile_parent_fake_control_environment_is_ignored(self) -> None:
        hostile_environment = {
            "FAILURE_COMMAND": "event_ledger.py",
            "FAILURE_RC": "97",
            "MARKOUT_RC": "98",
            "REPORT_RC": "99",
            "PIPELINE_CALL_LOG": "/tmp/hostile-call-log",
            "EVENT_EVIDENCE_PROJECT_ROOT": "/tmp/hostile-project-root",
            "PIPELINE_FAKE_REPORT_PATH": "/tmp/hostile-report-path",
        }
        with patch.dict(os.environ, hostile_environment, clear=False):
            result, calls, lock_exists, report_refreshed = self.run_pipeline()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            calls,
            ["event_ledger.py", "market_state_recorder.py", "markout_worker.py", "evidence_report.py"],
        )
        self.assertTrue(lock_exists)
        self.assertTrue(report_refreshed)

    def test_terminal_provider_failure_still_refreshes_report(self) -> None:
        result, calls, lock_exists, report_refreshed = self.run_pipeline(markout_rc=2)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            calls,
            ["event_ledger.py", "market_state_recorder.py", "markout_worker.py", "evidence_report.py"],
        )
        self.assertTrue(lock_exists)
        self.assertTrue(report_refreshed)

    def test_report_failure_overrides_clean_markout_status(self) -> None:
        result, calls, _lock_exists, _report_refreshed = self.run_pipeline(markout_rc=0, report_rc=7)

        self.assertEqual(result.returncode, 7)
        self.assertEqual(calls[-2:], ["markout_worker.py", "evidence_report.py"])

    def test_report_failure_overrides_terminal_provider_status(self) -> None:
        result, calls, _lock_exists, _report_refreshed = self.run_pipeline(markout_rc=2, report_rc=7)

        self.assertEqual(result.returncode, 7)
        self.assertEqual(calls[-2:], ["markout_worker.py", "evidence_report.py"])

    def test_ingestion_and_state_failures_stop_later_stages(self) -> None:
        cases = [
            ("event_ledger.py", 4, None, ["event_ledger.py"]),
            (
                "event_ledger.py --input reports/xtracker_strategy_decisions.jsonl",
                5,
                {"reports/xtracker_strategy_decisions.jsonl": "{}\n"},
                ["event_ledger.py", "event_ledger.py --input reports/xtracker_strategy_decisions.jsonl"],
            ),
            (
                "market_state_recorder.py",
                6,
                None,
                ["event_ledger.py", "market_state_recorder.py"],
            ),
        ]
        for failure_command, failure_rc, source_files, expected_calls in cases:
            with self.subTest(failure_command=failure_command):
                result, calls, _lock_exists, _report_refreshed = self.run_pipeline(
                    failure_command=failure_command,
                    failure_rc=failure_rc,
                    source_files=source_files,
                )
                self.assertEqual(result.returncode, failure_rc)
                self.assertEqual(calls, expected_calls)

    def test_unexpected_markout_status_fails_closed_without_report(self) -> None:
        result, calls, _lock_exists, report_refreshed = self.run_pipeline(markout_rc=9)

        self.assertEqual(result.returncode, 9)
        self.assertEqual(calls[-1], "markout_worker.py")
        self.assertNotIn("evidence_report.py", calls)
        self.assertFalse(report_refreshed)

    def test_lock_contention_runs_no_pipeline_stage(self) -> None:
        result, calls, lock_exists, report_refreshed = self.run_pipeline(hold_lock=True)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, [])
        self.assertTrue(lock_exists)
        self.assertFalse(report_refreshed)

    def test_command_order_and_conditional_source_ingestion(self) -> None:
        result, calls, _lock_exists, report_refreshed = self.run_pipeline(
            source_files={
                "reports/xtracker_strategy_decisions.jsonl": "{}\n",
                "reports/stock_price_strategy_decisions.jsonl": "",
            }
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            calls,
            [
                "event_ledger.py",
                "event_ledger.py --input reports/xtracker_strategy_decisions.jsonl",
                "market_state_recorder.py",
                "markout_worker.py",
                "evidence_report.py",
            ],
        )
        self.assertTrue(report_refreshed)


class EventLedgerTests(unittest.TestCase):
    def test_normalize_sec_event_keeps_precise_feed_time(self) -> None:
        signal = {
            "id": "sec-1",
            "source": "SEC_EDGAR",
            "source_group": "filings",
            "record_type": "8-K",
            "record_date": "2026-07-17",
            "source_published_at": "2026-07-17T14:01:02Z",
            "source_timestamp_precision": "exact_second",
            "fetched_at": "2026-07-17T14:01:05Z",
            "parsed_at": "2026-07-17T14:01:06Z",
            "detected_at": "2026-07-17T14:01:07Z",
            "ticker": "AAPL",
            "company": "APPLE INC",
            "headline": "8-K - APPLE INC",
            "version": "scanner-v1",
            "raw": {"accession": "0001", "updated": "2026-07-17T14:01:02Z"},
        }
        event = event_ledger.normalize_signal(signal)
        self.assertEqual(event["source_published_at"], "2026-07-17T14:01:02Z")
        self.assertEqual(event["source_timestamp_precision"], "exact_second")
        self.assertEqual(event["targets"]["stock"]["ticker"], "AAPL")
        self.assertTrue(event["raw_payload_hash"].startswith("sha256:"))
        self.assertEqual(event["latency_ms"]["source_to_first_seen"], 5000)

    def test_legacy_usaspending_is_conservatively_unverified(self) -> None:
        signal = {
            "id": "award-1",
            "source": "USAspending",
            "record_type": "contracts",
            "record_date": "2026-07-15 11:17:09",
            "record_date_type": "last_modified",
            "detected_at": "2026-07-17T14:00:00Z",
            "ticker": "BA",
            "raw": {"Award ID": "ABC"},
        }
        event = event_ledger.normalize_signal(signal)
        self.assertEqual(event["source_timestamp_precision"], "source_clock_unverified")
        self.assertEqual(event["eligible_markout_windows"], ["1d", "5d"])
        self.assertIn("legacy", event["timestamp_notes"].lower())

    def test_append_is_idempotent_by_event_id(self) -> None:
        signal = {
            "id": "same",
            "source": "SEC_EDGAR",
            "record_date": "2026-07-17",
            "detected_at": "2026-07-17T14:00:00Z",
            "raw": {},
        }
        with tempfile.TemporaryDirectory() as temp:
            input_path = Path(temp) / "signals.jsonl"
            output_path = Path(temp) / "ledger.jsonl"
            input_path.write_text(json.dumps(signal) + "\n" + json.dumps(signal) + "\n", encoding="utf-8")
            first = event_ledger.ingest(input_path, output_path)
            second = event_ledger.ingest(input_path, output_path)
            self.assertEqual(first["appended"], 1)
            self.assertEqual(second["appended"], 0)
            self.assertEqual(len(output_path.read_text(encoding="utf-8").splitlines()), 1)

    def test_explicit_adapter_revision_creates_immutable_revision(self) -> None:
        base = {
            "id": "same-source-record", "source": "SEC_EDGAR",
            "record_date": "2026-07-17", "detected_at": "2026-07-17T14:00:00Z", "raw": {},
        }
        original = event_ledger.normalize_signal(base)
        revised = event_ledger.normalize_signal({**base, "adapter_revision": "timestamp-fix-v2"})
        self.assertEqual(original["logical_event_id"], revised["logical_event_id"])
        self.assertNotEqual(original["event_id"], revised["event_id"])
        self.assertEqual(revised["revision_id"], "timestamp-fix-v2")

    def test_revision_content_is_bound_into_event_id(self) -> None:
        base = {
            "id": "same-source-record", "source": "SEC_EDGAR", "adapter_revision": "parser-v2",
            "record_date": "2026-07-17", "detected_at": "2026-07-17T14:00:00Z", "raw": {},
        }
        aapl = event_ledger.normalize_signal({**base, "ticker": "AAPL"})
        msft = event_ledger.normalize_signal({**base, "ticker": "MSFT"})
        self.assertNotEqual(aapl["event_id"], msft["event_id"])

    def test_ingest_auto_supersedes_prior_logical_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "signals.jsonl"
            output_path = Path(directory) / "ledger.jsonl"
            base = {
                "id": "same-source-record", "source": "SEC_EDGAR", "record_date": "2026-07-17",
                "detected_at": "2026-07-17T14:00:00Z", "ticker": "AAPL", "raw": {},
            }
            revised = {**base, "adapter_revision": "parser-v2", "ticker": "MSFT"}
            input_path.write_text("\n".join(json.dumps(row) for row in (base, revised)) + "\n", encoding="utf-8")
            result = event_ledger.ingest(input_path, output_path)
            rows = list(event_ledger.read_jsonl(output_path))
            active = event_ledger.active_events(rows)
            self.assertEqual(result["appended"], 2)
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["targets"]["stock"]["ticker"], "MSFT")
            self.assertEqual(active[0]["supersedes_event_id"], rows[0]["event_id"])


class MarketStateTests(unittest.TestCase):
    @staticmethod
    def yahoo_payload() -> dict:
        return {
            "chart": {"result": [{
                "meta": {"exchangeName": "NMS", "currency": "USD"},
                "timestamp": [1784296800, 1784296860],
                "indicators": {"quote": [{
                    "open": [100.0, 101.0], "high": [101.0, 102.0],
                    "low": [99.0, 100.5], "close": [100.5, 101.5],
                    "volume": [1000, 1100],
                }]},
            }], "error": None},
        }

    def test_yahoo_baseline_uses_only_completed_asof_bar(self) -> None:
        state = market_state_recorder.stock_state_from_yahoo(
            self.yahoo_payload(), ticker="AAPL", target_at="2026-07-17T14:01:30Z",
            observed_at="2026-07-17T14:02:00Z", selection_mode="baseline_asof", interval="1m",
        )
        self.assertEqual(state["last"], 100.5)
        self.assertEqual(state["market_timestamp"], "2026-07-17T14:01:00Z")
        self.assertTrue(state["valid_baseline"])
        self.assertIsNone(state["bid"])
        self.assertFalse(state["executable_quote"])
        self.assertEqual(state["quote_quality"], "historical_1m_bar_proxy")

    def test_yahoo_markout_is_at_or_after_horizon(self) -> None:
        payload = self.yahoo_payload()
        result = payload["chart"]["result"][0]
        result["timestamp"].append(1784296920)
        for key, value in {"open": 102.0, "high": 102.5, "low": 101.9, "close": 102.2, "volume": 900}.items():
            result["indicators"]["quote"][0][key].append(value)
        state = market_state_recorder.stock_state_from_yahoo(
            payload, ticker="AAPL", target_at="2026-07-17T14:01:30Z",
            observed_at="2026-07-17T14:04:00Z", selection_mode="markout_at_or_after", interval="1m",
        )
        self.assertEqual(state["last"], 102.2)
        self.assertEqual(state["bar_start"], "2026-07-17T14:02:00Z")
        self.assertEqual(state["market_timestamp"], "2026-07-17T14:03:00Z")
        self.assertGreaterEqual(state["target_offset_seconds"], 0)

    def test_parse_clob_book_keeps_depth_and_requires_positive_size(self) -> None:
        payload = {
            "bids": [{"price": "0.45", "size": "100"}, {"price": "0.46", "size": "25"}],
            "asks": [{"price": "0.49", "size": "40"}, {"price": "0.48", "size": "30"}],
        }
        state = market_state_recorder.polymarket_state_from_book(
            payload, token_id="tok", outcome="YES", observed_at="2026-07-17T14:02:00Z"
        )
        self.assertEqual(state["bid"], 0.46)
        self.assertEqual(state["ask"], 0.48)
        self.assertEqual(state["bid_size"], 25.0)
        self.assertEqual(state["ask_size"], 30.0)
        self.assertTrue(state["executable_quote"])
        zero = market_state_recorder.polymarket_state_from_book(
            {"bids": [{"price": "0.49", "size": "0"}], "asks": [{"price": "0.51", "size": "12"}]},
            token_id="tok", outcome="YES", observed_at="2026-07-17T14:02:00Z",
        )
        self.assertFalse(zero["executable_quote"])

    def test_buy_no_resolves_only_no_token_and_normalizes_to_buy(self) -> None:
        selected, normalized_side, desired_outcome, error = market_state_recorder.select_polymarket_tokens(
            {"paper_side": "buy_no", "paper_quantity": 5},
            [
                {"token_id": "yes-token", "outcome": "Yes", "mapping_verified": True},
                {"token_id": "no-token", "outcome": "No", "mapping_verified": True},
            ],
        )
        self.assertIsNone(error)
        self.assertEqual(selected, [{"token_id": "no-token", "outcome": "No", "mapping_verified": True}])
        self.assertEqual(normalized_side, "buy")
        self.assertEqual(desired_outcome, "no")

    def test_buy_requires_exactly_one_declared_outcome(self) -> None:
        selected, normalized_side, _outcome, error = market_state_recorder.select_polymarket_tokens(
            {"paper_side": "buy"},
            [
                {"token_id": "yes-token", "outcome": "Yes"},
                {"token_id": "no-token", "outcome": "No"},
            ],
        )
        self.assertEqual(selected, [])
        self.assertIsNone(normalized_side)
        self.assertIn("paper_outcome", error or "")

    def test_declared_outcome_rejects_unverified_token_mapping(self) -> None:
        selected, normalized_side, desired_outcome, error = market_state_recorder.select_polymarket_tokens(
            {"paper_side": "buy_yes"},
            [{"token_id": "claimed-yes", "outcome": "Yes", "mapping_verified": False}],
        )
        self.assertEqual(selected, [])
        self.assertIsNone(normalized_side)
        self.assertEqual(desired_outcome, "yes")
        self.assertIn("verified", str(error).lower())

    def test_gamma_market_selection_requires_exact_identity(self) -> None:
        payload = [
            {"id": "wrong", "conditionId": "0xwrong", "slug": "wrong-market"},
            {"id": "right", "conditionId": "0xabc", "slug": "right-market"},
        ]
        selected = market_state_recorder.select_gamma_market(payload, {"condition_id": "0xabc"})
        self.assertEqual(selected["id"], "right")


class MarkoutTests(unittest.TestCase):
    def test_stock_markout_separates_response_from_executability(self) -> None:
        baseline = {"asset_type": "stock", "last": 100.0, "bid": None, "ask": None}
        later = {"asset_type": "stock", "last": 102.0, "bid": None, "ask": None}
        row = markout_worker.calculate_markout(baseline, later)
        self.assertAlmostEqual(row["market_response_return"], 0.02)
        self.assertIsNone(row["executable_return"])
        self.assertFalse(row["capturable_evidence"])

    def test_polymarket_markout_requires_declared_side_quantity_and_size(self) -> None:
        baseline = {
            "asset_type": "polymarket", "last": 0.50, "bid": 0.49, "ask": 0.51,
            "bid_size": 20.0, "ask_size": 20.0, "executable_quote": True,
            "token_id": "yes-token", "mapping_verified": True,
        }
        later = {
            "asset_type": "polymarket", "last": 0.56, "bid": 0.55, "ask": 0.57,
            "bid_size": 20.0, "ask_size": 20.0, "executable_quote": True,
            "token_id": "yes-token",
        }
        undeclared = markout_worker.calculate_markout(baseline, later)
        self.assertFalse(undeclared["capturable_evidence"])
        baseline["paper_side"] = "buy"
        baseline["paper_quantity"] = 5.0
        row = markout_worker.calculate_markout(baseline, later)
        self.assertAlmostEqual(row["market_response_return"], 0.12)
        self.assertAlmostEqual(row["gross_top_of_book_return"], (0.55 - 0.51) / 0.51)
        self.assertTrue(row["gross_top_of_book_feasible"])
        self.assertFalse(row["capturable_evidence"])
        unverified = markout_worker.calculate_markout(
            {**baseline, "mapping_verified": False, "paper_side": "buy", "paper_quantity": 10}, later
        )
        self.assertFalse(unverified["gross_top_of_book_feasible"])

    def test_due_tasks_respect_precision_eligibility(self) -> None:
        baseline_time = datetime(2026, 7, 10, 14, 0, tzinfo=UTC)
        snapshot = {
            "snapshot_id": "snap-1",
            "event_id": "evt-1",
            "asset_type": "stock",
            "last": 100.0,
            "valid_baseline": True,
            "source_timestamp_precision": "date_only",
            "anchor_at": baseline_time.isoformat().replace("+00:00", "Z"),
            "observed_at": baseline_time.isoformat().replace("+00:00", "Z"),
            "eligible_markout_windows": ["1d", "5d"],
        }
        tasks = markout_worker.due_tasks(
            [snapshot], existing_keys=set(), now=baseline_time + timedelta(days=2)
        )
        self.assertEqual([task["window"] for task in tasks], ["1d"])

    def test_stock_markout_waits_for_completed_post_horizon_bar(self) -> None:
        anchor = datetime(2026, 7, 17, 16, 4, 59, tzinfo=UTC)
        snapshot = {
            "snapshot_id": "stock-grace", "event_id": "evt-grace", "asset_type": "stock",
            "last": 100.0, "valid_baseline": True, "source_timestamp_precision": "exact_second",
            "anchor_at": anchor.isoformat().replace("+00:00", "Z"),
            "eligible_markout_windows": ["1m"],
        }
        scheduled = anchor + timedelta(minutes=1)
        before_ready = markout_worker.due_tasks(
            [snapshot], existing_keys=set(),
            now=scheduled + timedelta(seconds=markout_worker.STOCK_BAR_COMPLETION_GRACE_SECONDS - 1),
        )
        at_ready = markout_worker.due_tasks(
            [snapshot], existing_keys=set(),
            now=scheduled + timedelta(seconds=markout_worker.STOCK_BAR_COMPLETION_GRACE_SECONDS),
        )
        self.assertEqual(before_ready, [])
        self.assertEqual(len(at_ready), 1)
        self.assertEqual(at_ready[0]["scheduled_for"], scheduled)
        self.assertEqual(
            at_ready[0]["ready_at"],
            scheduled + timedelta(seconds=markout_worker.STOCK_BAR_COMPLETION_GRACE_SECONDS),
        )

    def test_polymarket_markout_is_due_at_exact_horizon(self) -> None:
        anchor = datetime(2026, 7, 17, 16, 4, 59, tzinfo=UTC)
        snapshot = {
            "snapshot_id": "poly-no-grace", "event_id": "evt-poly", "asset_type": "polymarket",
            "bid": 0.48, "ask": 0.50, "valid_baseline": True,
            "source_timestamp_precision": "exact_second",
            "anchor_at": anchor.isoformat().replace("+00:00", "Z"),
            "eligible_markout_windows": ["1m"],
        }
        scheduled = anchor + timedelta(minutes=1)
        tasks = markout_worker.due_tasks([snapshot], existing_keys=set(), now=scheduled)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["ready_at"], scheduled)

    def test_due_tasks_deduplicate_duplicate_snapshot_rows(self) -> None:
        baseline_time = datetime(2026, 7, 10, 14, 0, tzinfo=UTC)
        snapshot = {
            "snapshot_id": "snap-duplicate", "event_id": "evt-1", "asset_type": "stock",
            "last": 100.0, "valid_baseline": True, "source_timestamp_precision": "date_only",
            "anchor_at": baseline_time.isoformat().replace("+00:00", "Z"),
            "eligible_markout_windows": ["1d"],
        }
        tasks = markout_worker.due_tasks(
            [snapshot, dict(snapshot)], existing_keys=set(), now=baseline_time + timedelta(days=2)
        )
        self.assertEqual(len(tasks), 1)

    def test_stock_intraday_windows_do_not_cross_regular_session_close(self) -> None:
        anchor = datetime(2026, 7, 17, 19, 22, 40, tzinfo=UTC)  # 3:22:40 p.m. ET
        snapshot = {
            "asset_type": "stock", "source_timestamp_precision": "exact_second",
            "anchor_at": anchor.isoformat().replace("+00:00", "Z"),
            "eligible_markout_windows": ["1m", "5m", "15m", "60m", "next_open", "1d", "5d"],
        }
        allowed = markout_worker.allowed_windows(snapshot)
        self.assertTrue({"1m", "5m", "15m", "next_open", "1d", "5d"} <= allowed)
        self.assertNotIn("60m", allowed)

    def test_provider_failure_is_terminal_and_not_retried_forever(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            states_path = Path(directory) / "states.jsonl"
            markouts_path = Path(directory) / "markouts.jsonl"
            snapshot = {
                "measurement_version": markout_worker.MEASUREMENT_VERSION,
                "snapshot_id": "s1", "event_id": "e1", "asset_type": "stock",
                "source_timestamp_precision": "exact_second", "valid_baseline": True,
                "anchor_at": "2026-07-17T14:00:00Z", "last": 100.0,
                "eligible_markout_windows": ["1m"],
            }
            states_path.write_text(json.dumps(snapshot) + "\n", encoding="utf-8")
            with patch.object(markout_worker, "fetch_later_state", side_effect=RuntimeError("provider down")):
                first = markout_worker.run_markouts(states_path, markouts_path, dry_run=False)
                second = markout_worker.run_markouts(states_path, markouts_path, dry_run=False)
            rows = list(event_ledger.read_jsonl(markouts_path))
            self.assertEqual(first["markouts_written"], 1)
            self.assertEqual(second["markouts_written"], 0)
            self.assertEqual(rows[0]["status"], "provider_unavailable")
            self.assertFalse(rows[0]["valid_timing"])
            self.assertIsNone(rows[0]["market_response_return"])

    def test_invalid_timing_clears_all_execution_values(self) -> None:
        calculations = {
            "entry_ask": 0.51, "exit_bid": 0.55,
            "gross_top_of_book_change": 0.04,
            "gross_top_of_book_return": 0.04 / 0.51,
            "gross_top_of_book_feasible": True,
            "executable_return": 0.04 / 0.51,
            "capturable_evidence": False,
        }
        markout_worker.clear_invalid_timing_execution(calculations)
        for key in (
            "entry_ask", "exit_bid", "gross_top_of_book_change",
            "gross_top_of_book_return", "executable_return",
        ):
            self.assertIsNone(calculations[key])
        self.assertFalse(calculations["gross_top_of_book_feasible"])
        self.assertFalse(calculations["capturable_evidence"])


class EvidenceReportTests(unittest.TestCase):
    def test_report_does_not_claim_edge_without_executable_markouts(self) -> None:
        events = [{"event_id": "e1", "source": "SEC_EDGAR", "source_timestamp_precision": "exact_second"}]
        snapshots = [{"snapshot_id": "s1", "event_id": "e1", "quote_quality": "historical_1m_bar_proxy", "executable_quote": False}]
        markouts = [
            {
                "event_id": "e1", "snapshot_id": "s1", "window": "5m",
                "market_response_return": 0.02, "executable_return": None,
                "capturable_evidence": False, "valid_timing": True,
            },
            {
                "event_id": "e1", "snapshot_id": "s1", "window": "5m",
                "market_response_return": 0.03, "executable_return": None,
                "capturable_evidence": False, "valid_timing": False,
            },
        ]
        report = evidence_report.build_report(events, snapshots, markouts, generated_at="2026-07-17T15:00:00Z")
        self.assertEqual(report["conclusion"], "market_response_observed_but_capturable_edge_unproven")
        self.assertEqual(report["counts"]["capturable_markouts"], 0)
        self.assertEqual(report["aggregates"][0]["market_response"]["n"], 1)

    def test_shared_market_bars_count_as_one_independent_observation(self) -> None:
        events = [
            {"event_id": "e1", "source": "SEC_EDGAR", "event_type": "8-K", "source_timestamp_precision": "exact_second"},
            {"event_id": "e2", "source": "SEC_EDGAR", "event_type": "8-K", "source_timestamp_precision": "exact_second"},
        ]
        snapshots = [
            {"snapshot_id": "s1", "event_id": "e1", "ticker": "AAPL", "valid_baseline": True},
            {"snapshot_id": "s2", "event_id": "e2", "ticker": "AAPL", "valid_baseline": True},
        ]
        common = {
            "window": "5m", "asset_type": "stock", "valid_timing": True,
            "market_response_return": 0.02, "gross_top_of_book_feasible": False,
            "baseline_state": {"ticker": "AAPL", "market_timestamp": "2026-07-17T14:01:00Z"},
            "later_state": {"ticker": "AAPL", "market_timestamp": "2026-07-17T14:07:00Z"},
        }
        markouts = [
            {**common, "event_id": "e1", "snapshot_id": "s1", "anchor_at": "2026-07-17T14:01:10Z", "scheduled_for": "2026-07-17T14:06:10Z"},
            {**common, "event_id": "e2", "snapshot_id": "s2", "anchor_at": "2026-07-17T14:01:20Z", "scheduled_for": "2026-07-17T14:06:20Z"},
        ]
        report = evidence_report.build_report(events, snapshots, markouts)
        self.assertEqual(report["counts"]["valid_market_response_markouts"], 2)
        self.assertEqual(report["counts"]["independent_valid_response_observations"], 1)
        self.assertEqual(report["aggregates"][0]["independent_observations"], 1)

    def test_polymarket_expected_horizons_exclude_next_open(self) -> None:
        events = [{
            "event_id": "e1", "source": "SEC_EDGAR", "source_timestamp_precision": "exact_second",
            "targets": {"stock": None, "polymarket": [{"token_id": "yes-token"}]},
        }]
        snapshots = [{
            "snapshot_id": "s1", "event_id": "e1", "asset_type": "polymarket",
            "valid_baseline": True, "source_timestamp_precision": "exact_second",
            "anchor_at": "2026-07-17T14:00:00Z", "last": 0.50,
            "eligible_markout_windows": ["1m", "5m", "15m", "60m", "next_open", "1d", "5d"],
        }]
        report = evidence_report.build_report(events, snapshots, [])
        self.assertEqual(report["counts"]["expected_markout_horizons"], 6)
        self.assertEqual(report["counts"]["pending_markout_horizons"], 6)

    def test_price_less_baseline_has_no_expected_horizons(self) -> None:
        events = [{"event_id": "e1", "source": "SEC_EDGAR", "source_timestamp_precision": "exact_second"}]
        snapshots = [{
            "snapshot_id": "s1", "event_id": "e1", "asset_type": "polymarket",
            "valid_baseline": True, "source_timestamp_precision": "exact_second",
            "anchor_at": "2026-07-17T14:00:00Z", "last": None, "bid": None, "ask": None,
            "eligible_markout_windows": ["1m", "5m", "1d"],
        }]
        report = evidence_report.build_report(events, snapshots, [])
        self.assertEqual(report["counts"]["expected_markout_horizons"], 0)
        self.assertEqual(report["counts"]["pending_markout_horizons"], 0)


class PolymarketMatchingTests(unittest.TestCase):
    def test_short_common_word_ticker_is_not_a_match(self) -> None:
        score = public_record_reaction_bot.market_match_score(
            {"ticker": "ON", "company": "ON Semiconductor Corporation", "source": "SEC_EDGAR"},
            {"question": "Will tariffs remain on China?", "slug": "tariffs-on-china"},
        )
        self.assertEqual(score, 0.0)

    def test_short_ticker_does_not_prefix_match_longer_dollar_symbol(self) -> None:
        score = public_record_reaction_bot.market_match_score(
            {"ticker": "C", "company": "Citigroup Inc", "source": "SEC_EDGAR"},
            {"question": "Will $CPI exceed 3%?", "slug": "cpi-above-three"},
        )
        self.assertEqual(score, 0.0)

    def test_sec_substring_is_not_a_match(self) -> None:
        score = public_record_reaction_bot.market_match_score(
            {"ticker": "XYZ", "company": "Example Holdings", "source": "SEC_EDGAR"},
            {"question": "Will second quarter CPI exceed 3%?", "slug": "second-quarter-cpi"},
        )
        self.assertEqual(score, 0.0)

    def test_explicit_dollar_ticker_is_a_strong_match(self) -> None:
        score = public_record_reaction_bot.market_match_score(
            {"ticker": "AAPL", "company": "Apple Inc", "source": "SEC_EDGAR"},
            {"question": "Will $AAPL exceed 250 this year?", "slug": "aapl-250"},
        )
        self.assertEqual(score, 1.0)


if __name__ == "__main__":
    unittest.main()
