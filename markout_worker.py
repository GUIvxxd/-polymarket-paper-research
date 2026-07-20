#!/usr/bin/env python3
"""Create causal, idempotent v2 markouts for event-evidence baselines."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import event_ledger
import market_state_recorder as market

ROOT = Path(__file__).resolve().parent
MEASUREMENT_VERSION = "event_evidence_v2_2026_07_17"
DEFAULT_EVENTS = ROOT / "reports" / "event_evidence_ledger_v2.jsonl"
DEFAULT_STATES = ROOT / "reports" / "event_market_states_v2.jsonl"
DEFAULT_MARKOUTS = ROOT / "reports" / "event_markouts_v2.jsonl"
WINDOW_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "60m": 3600, "1d": 86400, "5d": 432000}
INTRADAY_ALLOWED = {"1m", "5m", "15m", "60m", "next_open", "1d", "5d"}
CONSERVATIVE_ALLOWED = {"1d", "5d"}
INTRADAY_SESSION_WINDOWS = {"1m", "5m", "15m", "60m"}
# Yahoo markouts select a completed bar whose start is at/after the horizon.
# At the horizon itself that bar cannot yet be complete, so defer stock work.
STOCK_BAR_COMPLETION_GRACE_SECONDS = 120


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def reference_price(state: dict[str, Any]) -> float | None:
    bid, ask = number(state.get("bid")), number(state.get("ask"))
    if bid is not None and ask is not None:
        return (bid + ask) / 2
    return number(state.get("last"))


def _positive(value: Any) -> float | None:
    value = number(value)
    return value if value is not None and value > 0 else None


def calculate_markout(baseline: dict[str, Any], later: dict[str, Any]) -> dict[str, Any]:
    baseline_ref, later_ref = reference_price(baseline), reference_price(later)
    response_change = later_ref - baseline_ref if baseline_ref is not None and later_ref is not None else None
    response_return = response_change / baseline_ref if response_change is not None and baseline_ref not in (None, 0) else None

    side = str(baseline.get("paper_side") or "").lower()
    quantity = _positive(baseline.get("paper_quantity"))
    entry_ask, exit_bid = number(baseline.get("ask")), number(later.get("bid"))
    entry_size, exit_size = _positive(baseline.get("ask_size")), _positive(later.get("bid_size"))
    same_token = bool(baseline.get("token_id") and baseline.get("token_id") == later.get("token_id"))
    executable_states = baseline.get("executable_quote") is True and later.get("executable_quote") is True
    declared_long = side in {"buy", "buy_yes", "long"} and baseline.get("mapping_verified") is True
    gross_feasible = bool(
        executable_states and declared_long and same_token and quantity is not None
        and entry_ask is not None and entry_ask > 0 and exit_bid is not None
        and entry_size is not None and exit_size is not None
        and entry_size >= quantity and exit_size >= quantity
    )
    gross_change = exit_bid - entry_ask if gross_feasible else None
    gross_return = gross_change / entry_ask if gross_change is not None and entry_ask else None
    return {
        "baseline_reference_price": baseline_ref,
        "later_reference_price": later_ref,
        "market_response_change": response_change,
        "market_response_return": response_return,
        "paper_side": side or None,
        "paper_quantity": quantity,
        "entry_ask": entry_ask if gross_feasible else None,
        "exit_bid": exit_bid if gross_feasible else None,
        "gross_top_of_book_change": gross_change,
        "gross_top_of_book_return": gross_return,
        "gross_top_of_book_feasible": gross_feasible,
        "executable_return": gross_return,
        "capturable_evidence": False,
        "capturable_limitation": "Fees, slippage, queue position, fill probability, and settlement costs are not modeled.",
    }


def clear_invalid_timing_execution(calculations: dict[str, Any]) -> None:
    """Remove execution-looking values when the observation missed its horizon."""
    for key in (
        "entry_ask", "exit_bid", "gross_top_of_book_change",
        "gross_top_of_book_return", "executable_return",
    ):
        calculations[key] = None
    calculations["gross_top_of_book_feasible"] = False
    calculations["capturable_evidence"] = False


def next_stock_open(anchor: datetime) -> datetime:
    eastern = anchor.astimezone(ZoneInfo("America/New_York"))
    candidate = eastern.replace(hour=9, minute=30, second=0, microsecond=0)
    if candidate <= eastern:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def scheduled_time(anchor: datetime, window: str, asset_type: str) -> datetime | None:
    if window in WINDOW_SECONDS:
        return anchor + timedelta(seconds=WINDOW_SECONDS[window])
    if window == "next_open" and asset_type == "stock":
        return next_stock_open(anchor)
    return None


def stock_intraday_horizon_available(scheduled: datetime) -> bool:
    """Return whether a 1-minute bar can begin at/after the horizon before close.

    This uses the regular Monday-Friday 09:30-16:00 ET session. Exchange holidays
    remain provider-observed unavailability rather than being guessed here.
    """
    eastern = scheduled.astimezone(ZoneInfo("America/New_York"))
    if eastern.weekday() >= 5:
        return False
    session_start = eastern.replace(hour=9, minute=30, second=0, microsecond=0)
    latest_bar_start = eastern.replace(hour=15, minute=59, second=0, microsecond=0)
    return session_start <= eastern <= latest_bar_start


def allowed_windows(snapshot: dict[str, Any]) -> set[str]:
    precision = snapshot.get("source_timestamp_precision")
    allowed = set(INTRADAY_ALLOWED if precision in {"exact_second", "exact_millisecond", "minute"} else CONSERVATIVE_ALLOWED)
    asset_type = str(snapshot.get("asset_type") or "")
    if asset_type != "stock":
        allowed.discard("next_open")
    requested = {str(value) for value in snapshot.get("eligible_markout_windows") or []}
    allowed &= requested
    if asset_type == "stock":
        anchor = market.parse_utc(snapshot.get("anchor_at"))
        if anchor is not None:
            for window in INTRADAY_SESSION_WINDOWS & set(allowed):
                scheduled = scheduled_time(anchor, window, asset_type)
                if scheduled is None or not stock_intraday_horizon_available(scheduled):
                    allowed.discard(window)
    return allowed


def task_ready_at(snapshot: dict[str, Any], scheduled: datetime) -> datetime:
    """Earliest safe fetch time for a completed post-horizon observation."""
    if snapshot.get("asset_type") == "stock":
        return scheduled + timedelta(seconds=STOCK_BAR_COMPLETION_GRACE_SECONDS)
    return scheduled


def due_tasks(
    snapshots: list[dict[str, Any]], *, existing_keys: set[str], now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    tasks: list[dict[str, Any]] = []
    for snapshot in snapshots:
        if snapshot.get("valid_baseline") is not True or reference_price(snapshot) is None:
            continue
        snapshot_id = str(snapshot.get("snapshot_id") or "")
        anchor = market.parse_utc(snapshot.get("anchor_at"))
        if not snapshot_id or anchor is None:
            continue
        for window in sorted(allowed_windows(snapshot), key=lambda item: (scheduled_time(anchor, item, str(snapshot.get("asset_type"))) or now)):
            key = f"{MEASUREMENT_VERSION}|{snapshot_id}|{window}"
            if key in existing_keys:
                continue
            scheduled = scheduled_time(anchor, window, str(snapshot.get("asset_type")))
            if scheduled is not None and task_ready_at(snapshot, scheduled) <= now:
                tasks.append({
                    "markout_key": key,
                    "snapshot": snapshot,
                    "window": window,
                    "scheduled_for": scheduled,
                    "ready_at": task_ready_at(snapshot, scheduled),
                })
                existing_keys.add(key)
    return tasks


def _state_subset(state: dict[str, Any]) -> dict[str, Any]:
    return {key: state.get(key) for key in (
        "provider", "quote_quality", "ticker", "token_id", "outcome", "market_timestamp",
        "bar_start", "bar_end", "last", "mid", "bid", "ask", "bid_size", "ask_size",
        "executable_quote", "paper_side", "paper_outcome", "paper_quantity",
        "mapping_verified", "verified_market_id", "verified_condition_id", "verified_slug",
    )}


def fetch_later_state(snapshot: dict[str, Any], scheduled_for: datetime, observed_at: str) -> dict[str, Any]:
    target_at = scheduled_for.isoformat(timespec="seconds").replace("+00:00", "Z")
    asset = snapshot.get("asset_type")
    if asset == "stock":
        ticker = str(snapshot.get("ticker") or "")
        payload, _range, interval = market.fetch_yahoo_chart(ticker, target_at, observed_at)
        state = market.stock_state_from_yahoo(
            payload, ticker=ticker, target_at=target_at, observed_at=observed_at,
            interval=interval, selection_mode="markout_at_or_after",
        )
        state["bar_interval"] = interval
        return state
    if asset == "polymarket":
        token_id = str(snapshot.get("token_id") or "")
        request_started = utc_now()
        payload = market.get_json(f"{market.CLOB}/book", {"token_id": token_id}, timeout=15)
        received = utc_now()
        return market.polymarket_state_from_book(
            payload, token_id=token_id, outcome=snapshot.get("outcome"), observed_at=received,
            target_at=target_at, request_started_at=request_started,
        )
    raise ValueError(f"unsupported asset_type: {asset}")


def run_markouts(
    states_path: Path, markouts_path: Path, *, events_path: Path | None = None, dry_run: bool = False,
) -> dict[str, Any]:
    snapshots = [row for row in event_ledger.read_jsonl(states_path)
                 if row.get("measurement_version") == MEASUREMENT_VERSION]
    if events_path is not None:
        active_ids = {
            str(row.get("event_id")) for row in event_ledger.active_events(
                row for row in event_ledger.read_jsonl(events_path)
                if row.get("measurement_version") == MEASUREMENT_VERSION
            )
        }
        snapshots = [row for row in snapshots if str(row.get("event_id")) in active_ids]
    existing = {str(row.get("markout_key")) for row in event_ledger.read_jsonl(markouts_path)
                if row.get("measurement_version") == MEASUREMENT_VERSION and row.get("markout_key")}
    tasks = due_tasks(snapshots, existing_keys=existing)
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for task in tasks:
        snapshot, scheduled = task["snapshot"], task["scheduled_for"]
        observed_at = utc_now()
        try:
            later = fetch_later_state(snapshot, scheduled, observed_at)
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            errors.append(f"{task['markout_key']}: {error_text}")
            calculations = calculate_markout(snapshot, {})
            clear_invalid_timing_execution(calculations)
            digest = hashlib.sha256(task["markout_key"].encode()).hexdigest()[:24]
            row = {
                "schema_version": 2, "measurement_version": MEASUREMENT_VERSION,
                "markout_id": f"mkv_{digest}", "markout_key": task["markout_key"],
                "snapshot_id": snapshot.get("snapshot_id"), "event_id": snapshot.get("event_id"),
                "logical_event_id": snapshot.get("logical_event_id"),
                "source": snapshot.get("source"), "event_type": snapshot.get("event_type"),
                "source_timestamp_precision": snapshot.get("source_timestamp_precision"),
                "asset_type": snapshot.get("asset_type"), "window": task["window"],
                "anchor_at": snapshot.get("anchor_at"),
                "scheduled_for": scheduled.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "recorded_at": observed_at, "timing_lag_seconds": None,
                "timing_tolerance_seconds": None, "valid_timing": False,
                "status": "provider_unavailable", "provider_error": error_text[:500],
                "baseline_state": _state_subset(snapshot), "later_state": {},
                **calculations,
            }
            rows.append(row)
            existing.add(task["markout_key"])
            continue
        market_dt = market.parse_utc(later.get("market_timestamp"))
        signed_lag = (market_dt - scheduled).total_seconds() if market_dt else None
        if snapshot.get("asset_type") == "stock":
            tolerance = market.interval_seconds(str(later.get("bar_interval") or "1m")) + 120
        else:
            tolerance = 180
        valid_timing = signed_lag is not None and 0 <= signed_lag <= tolerance
        calculations = calculate_markout(snapshot, later)
        if not valid_timing:
            clear_invalid_timing_execution(calculations)
        digest = hashlib.sha256(task["markout_key"].encode()).hexdigest()[:24]
        row = {
            "schema_version": 2, "measurement_version": MEASUREMENT_VERSION,
            "markout_id": f"mkv_{digest}", "markout_key": task["markout_key"],
            "snapshot_id": snapshot.get("snapshot_id"), "event_id": snapshot.get("event_id"),
            "logical_event_id": snapshot.get("logical_event_id"),
            "source": snapshot.get("source"), "event_type": snapshot.get("event_type"),
            "source_timestamp_precision": snapshot.get("source_timestamp_precision"),
            "asset_type": snapshot.get("asset_type"), "window": task["window"],
            "anchor_at": snapshot.get("anchor_at"),
            "scheduled_for": scheduled.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "recorded_at": observed_at, "timing_lag_seconds": signed_lag,
            "timing_tolerance_seconds": tolerance, "valid_timing": valid_timing,
            "status": "completed" if valid_timing else "timing_miss",
            "baseline_state": _state_subset(snapshot), "later_state": _state_subset(later),
            **calculations,
        }
        rows.append(row)
        existing.add(task["markout_key"])
    if rows and not dry_run:
        markouts_path.parent.mkdir(parents=True, exist_ok=True)
        with markouts_path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(event_ledger.canonical_json(row) + "\n")
    return {
        "measurement_version": MEASUREMENT_VERSION, "snapshots": len(snapshots),
        "tasks_due": len(tasks), "markouts_written": len(rows),
        "valid_markouts": sum(row.get("valid_timing") is True for row in rows),
        "gross_top_of_book_feasible": sum(row.get("gross_top_of_book_feasible") is True for row in rows),
        "capturable_markouts": 0, "errors_count": len(errors), "errors": errors[:20],
        "markouts_path": str(markouts_path), "dry_run": dry_run,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run due causal event-evidence markouts")
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--states", type=Path, default=DEFAULT_STATES)
    parser.add_argument("--markouts", type=Path, default=DEFAULT_MARKOUTS)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        result = run_markouts(args.states, args.markouts, events_path=args.events, dry_run=args.dry_run)
    except Exception as exc:
        print(f"markout worker error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(event_ledger.canonical_json(result))
    if result.get("errors_count"):
        print(f"markout worker recorded {result['errors_count']} terminal provider-unavailable markout(s)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
