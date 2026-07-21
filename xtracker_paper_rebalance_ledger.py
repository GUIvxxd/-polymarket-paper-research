#!/usr/bin/env python3
"""Paper-only rebalance ledger/backtest for xtracker Tweet/Post markets.

This replays the historical watchdog snapshots and compares two strategies:

1. Hold-to-resolution baseline: enter the first actionable bucket per event and
   hold until final count settlement.
2. Rebalance strategy: enter the first actionable bucket per event, then mark a
   paper exit when the stored snapshots show an executable bid that meets the
   exit/rebalance rules. If a stronger bucket is visible in the same snapshot,
   the strategy opens that new bucket at its ask.

Important limitation: xtracker_tweet_snapshots.jsonl only stores the watchdog's
observed candidate rows, not the full historical book for every bucket. This
means exits are conservative/observable-only; if a held bucket disappeared from
snapshots, this script does not invent a bid.

Public-data only: no X API, no wallet, no live orders.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import math
import re
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import strategy_evidence

ROOT = Path("/data/workspace/polymarket-research")
REPORTS = ROOT / "reports"
SNAPSHOTS = REPORTS / "xtracker_tweet_snapshots.jsonl"
PROOF = REPORTS / "xtracker_paper_proof_latest.json"
OUT_LEDGER_CSV = REPORTS / "xtracker_rebalance_paper_ledger_latest.csv"
OUT_TRADES_CSV = REPORTS / "xtracker_rebalance_paper_trades_latest.csv"
OUT_SUMMARY_MD = REPORTS / "xtracker_rebalance_paper_summary_latest.md"
OUT_JSON = REPORTS / "xtracker_rebalance_paper_summary_latest.json"
OUT_XLSX = REPORTS / "xtracker_rebalance_paper_ledger_latest.xlsx"
OUT_LIFECYCLE_CSV = REPORTS / "xtracker_rebalance_lifecycle_accounting_latest.csv"
OUT_LIFECYCLE_JSON = REPORTS / "xtracker_rebalance_lifecycle_accounting_latest.json"
STRATEGY_SIGNALS = REPORTS / "xtracker_strategy_decisions.jsonl"

# Match current tightened watchdog thresholds.
FILTER_VERSION = "tightened_v2_2026_07_15"
MIN_EDGE = 0.35
MIN_FAIR = 0.60
MIN_QTY = 20.0
MIN_COST_LOW = 2.0
MIN_COST_NORMAL = 5.0
MAX_ASK = 0.35
MAX_ENTRY_REMAINING_HOURS = 100.0
EARLY_LOW_BUCKET_REMAINING_HOURS = 48.0

# Rebalance/exit rules.
MIN_ABSOLUTE_PROFIT_EXIT = 0.03
MIN_RELATIVE_PROFIT_EXIT = 0.20
FAIR_COLLAPSE_THRESHOLD = 0.20
STALE_BID_EDGE = 0.10
BETTER_BUCKET_EDGE_DELTA = 0.10
REBALANCE_MIN_EDGE = 0.50
REBALANCE_MIN_FAIR = 0.70
REBALANCE_MAX_ASK = 0.25
STAKE_PER_TRADE = 100.0  # paper shares; PnL is linear, so ROI is unaffected.
LEGACY_PNL_VALIDITY = {
    "execution_valid_pnl": False,
    "net_capturable": False,
    "paper_pnl_basis": "legacy_xtracker_sparse_snapshot_top_of_book_100_shares",
    "invalid_reasons": [
        "sparse_watchdog_snapshots_not_full_historical_books",
        "zero_fee_legacy_assumption",
        "no_post_decision_latency_fill_book",
        "no_depth_walk_or_queue_model",
        "historical_v2_filter_not_current_v4_forward_protocol",
    ],
}

MONTHS = {
    "January": 1,
    "February": 2,
    "March": 3,
    "April": 4,
    "May": 5,
    "June": 6,
    "July": 7,
    "August": 8,
    "September": 9,
    "October": 10,
    "November": 11,
    "December": 12,
}


@dataclass
class Position:
    position_id: int
    event: str
    handle: str
    bucket: str
    question: str
    entry_time: str
    entry_price: float
    entry_fair: float | None
    entry_edge: float | None
    entry_count: int | None
    entry_projected: float | None
    yes_token_id: str | None
    condition_id: str | None
    source: str
    quantity: float = STAKE_PER_TRADE
    entry_bid: float | None = None
    entry_bid_size: float | None = None
    entry_ask_size: float | None = None
    entry_book_bids: list[Any] | None = None
    entry_book_asks: list[Any] | None = None
    entry_book_request_started_at: str | None = None
    entry_book_response_received_at: str | None = None
    entry_book_provider_timestamp: str | None = None
    entry_book_timing_quality: str | None = None
    latest_bid: float | None = None
    latest_bid_size: float | None = None
    latest_ask: float | None = None
    latest_ask_size: float | None = None


@dataclass
class ClosedTrade:
    position_id: int
    event: str
    handle: str
    bucket: str
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    exit_reason: str
    pnl_per_share: float
    roi_on_entry: float
    paper_pnl: float
    entry_count: int | None
    exit_count: int | None
    entry_fair: float | None
    exit_fair: float | None
    entry_edge: float | None
    exit_edge: float | None
    final_count: int | None
    won_if_settled: bool | None
    question: str | None = None
    yes_token_id: str | None = None
    condition_id: str | None = None
    source: str | None = None
    quantity: float = STAKE_PER_TRADE
    entry_bid: float | None = None
    entry_bid_size: float | None = None
    entry_ask_size: float | None = None
    entry_book_bids: list[Any] | None = None
    entry_book_asks: list[Any] | None = None
    entry_book_request_started_at: str | None = None
    entry_book_response_received_at: str | None = None
    entry_book_provider_timestamp: str | None = None
    entry_book_timing_quality: str | None = None
    exit_bid_size: float | None = None
    exit_ask: float | None = None
    exit_ask_size: float | None = None
    exit_book_bids: list[Any] | None = None
    exit_book_asks: list[Any] | None = None
    exit_book_request_started_at: str | None = None
    exit_book_response_received_at: str | None = None
    exit_book_provider_timestamp: str | None = None
    exit_book_timing_quality: str | None = None
    gross_top_of_book_feasible: bool = False
    execution_evidence_eligible: bool = False
    execution_valid_pnl: bool = False
    net_capturable: bool = False
    pnl_validity_reason: str = "legacy_xtracker_sparse_snapshot_top_of_book_zero_fee_no_latency"


def parse_time(raw: str) -> dt.datetime:
    return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def fnum(x: Any) -> float | None:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def inum(x: Any) -> int | None:
    try:
        if x is None:
            return None
        return int(x)
    except Exception:
        return None


def event_key(row: dict[str, Any] | Position) -> str:
    if isinstance(row, Position):
        return row.event
    return str(row.get("event") or row.get("question") or "")


def candidate_key(row: dict[str, Any] | Position) -> str:
    if isinstance(row, Position):
        return "|".join([row.event, row.handle, row.bucket, row.question])
    return "|".join([
        str(row.get("event", "")),
        str(row.get("handle", "")),
        str(row.get("bucket", "")),
        str(row.get("question", "")),
    ])


def best_bid(row: dict[str, Any]) -> float | None:
    book = row.get("best_bid_book")
    if isinstance(book, list) and book:
        return fnum(book[0])
    return fnum(row.get("bid"))


def best_ask(row: dict[str, Any]) -> float | None:
    book = row.get("best_ask_book")
    if isinstance(book, list) and book:
        return fnum(book[0])
    return fnum(row.get("ask"))


def best_level_size(row: dict[str, Any], side: str) -> float | None:
    book = row.get(f"best_{side}_book")
    if isinstance(book, (list, tuple)) and len(book) >= 2:
        return fnum(book[1])
    return fnum(row.get(f"{side}_size"))


def row_book_details(row: dict[str, Any], fallback_time: str) -> dict[str, Any]:
    request_at = row.get("book_request_started_at")
    response_at = row.get("book_response_received_at")
    timing_quality = row.get("book_timing_quality")
    if not timing_quality:
        timing_quality = "exact_request_response" if request_at and response_at else "snapshot_timestamp_only"
    bids = row.get("top_bids") or ([row.get("best_bid_book")] if row.get("best_bid_book") else [])
    asks = row.get("top_asks") or ([row.get("best_ask_book")] if row.get("best_ask_book") else [])
    return {
        "bids": bids,
        "asks": asks,
        "request_started_at": request_at,
        "response_received_at": response_at or fallback_time,
        "provider_timestamp": row.get("book_provider_timestamp"),
        "timing_quality": timing_quality,
    }


def depth_for_candidate(row: dict[str, Any]) -> tuple[float | None, dict[str, Any] | None]:
    ask = fnum(row.get("ask"))
    if ask is None:
        return None, None
    if ask <= 0.06:
        cap = "0.06"
    elif ask <= 0.10:
        cap = "0.1"
    elif ask <= 0.20:
        cap = "0.2"
    else:
        cap = "0.5"
    depth_by_cap = row.get("ask_depth_by_cap") or {}
    depth = depth_by_cap.get(cap) or row.get("depth")
    return float(cap), depth


def is_actionable(row: dict[str, Any]) -> tuple[bool, str]:
    # Prefer the watchdog's own actionability flag when present, but still
    # enforce max ask and core safety thresholds here.
    edge = fnum(row.get("edge"))
    fair = fnum(row.get("fair"))
    ask = best_ask(row) or fnum(row.get("ask"))
    remaining = fnum(row.get("remaining_hours"))
    if edge is None or fair is None or ask is None:
        return False, "missing_price_or_model"
    if row.get("confidence") == "low":
        return False, "low_confidence"
    if remaining is not None and remaining > MAX_ENTRY_REMAINING_HOURS:
        return False, f"remaining_hours_above_{MAX_ENTRY_REMAINING_HOURS}"
    if row.get("confidence") != "medium_high" and remaining is not None and remaining > 72.0:
        return False, "needs_medium_high_or_late_window"
    parsed = parse_bucket(str(row.get("bucket") or ""))
    if parsed:
        lo, hi = parsed
        if lo is None and hi is not None and hi <= 19 and remaining is not None and remaining > EARLY_LOW_BUCKET_REMAINING_HOURS:
            return False, "low_under_bucket_too_early"
    if ask > MAX_ASK:
        return False, "ask_above_max"
    if edge < MIN_EDGE:
        return False, "edge_below_threshold"
    if fair < MIN_FAIR:
        return False, "fair_below_threshold"
    if not row.get("best_ask_book"):
        return False, "missing_book_ask"
    _cap, depth = depth_for_candidate(row)
    if not depth:
        return False, "missing_depth"
    qty = fnum(depth.get("qty")) or 0.0
    cost = fnum(depth.get("cost")) or 0.0
    min_cost = MIN_COST_LOW if ask <= 0.10 else MIN_COST_NORMAL
    if qty < MIN_QTY:
        return False, f"depth_qty_below_{MIN_QTY}"
    if cost < min_cost:
        return False, f"depth_cost_below_{min_cost}"
    return True, "actionable"


def is_rebalance_entry(row: dict[str, Any]) -> tuple[bool, str]:
    ok, note = is_actionable(row)
    if not ok:
        return False, note
    edge = fnum(row.get("edge")) or 0.0
    fair = fnum(row.get("fair")) or 0.0
    ask = best_ask(row) or fnum(row.get("ask")) or 999.0
    if edge < REBALANCE_MIN_EDGE:
        return False, "rebalance_edge_below_threshold"
    if fair < REBALANCE_MIN_FAIR:
        return False, "rebalance_fair_below_threshold"
    if ask > REBALANCE_MAX_ASK:
        return False, "rebalance_ask_above_max"
    return True, "rebalance_actionable"


def parse_bucket(bucket: str) -> tuple[int | None, int | None] | None:
    s = (bucket or "").replace("\\u003c", "<").strip()
    if s.startswith("<"):
        try:
            return None, int(s[1:]) - 1
        except Exception:
            return None
    if s.endswith("+"):
        try:
            return int(s[:-1]), None
        except Exception:
            return None
    if "-" in s:
        try:
            lo, hi = s.split("-", 1)
            return int(lo), int(hi)
        except Exception:
            return None
    return None


def bucket_hit(bucket: str, count: int) -> bool | None:
    parsed = parse_bucket(bucket)
    if not parsed:
        return None
    lo, hi = parsed
    if lo is not None and count < lo:
        return False
    if hi is not None and count > hi:
        return False
    return True


def parse_window(event: str) -> tuple[dt.datetime, dt.datetime] | None:
    m = re.search(
        r"(?P<m1>January|February|March|April|May|June|July|August|September|October|November|December)\s+"
        r"(?P<d1>\d{1,2})\s*-\s*"
        r"(?:(?P<m2>January|February|March|April|May|June|July|August|September|October|November|December)\s+)?"
        r"(?P<d2>\d{1,2}),\s*(?P<y>\d{4})",
        event or "",
    )
    if not m:
        return None
    year = int(m.group("y"))
    month1 = MONTHS[m.group("m1")]
    day1 = int(m.group("d1"))
    month2 = MONTHS[m.group("m2") or m.group("m1")]
    day2 = int(m.group("d2"))
    start = dt.datetime(year, month1, day1, 16, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(year, month2, day2, 15, 59, 59, tzinfo=dt.timezone.utc)
    return start, end


def load_final_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    if not PROOF.exists():
        return counts
    proof = json.loads(PROOF.read_text())
    for entry in proof.get("all_entries", []) + proof.get("resolved_entries", []):
        event = entry.get("event")
        count = inum(entry.get("final_count"))
        if event and count is not None:
            counts[event] = count
    return counts


def load_snapshots() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in SNAPSHOTS.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        try:
            rec["_time"] = parse_time(rec["generated_at"])
        except Exception:
            continue
        records.append(rec)
    return sorted(records, key=lambda r: r["_time"])


def event_rows(snapshot: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    by_event: dict[str, list[dict[str, Any]]] = {}
    for row in snapshot.get("rows") or []:
        by_event.setdefault(event_key(row), []).append(row)
    return by_event


def best_actionable_row(rows: list[dict[str, Any]], exclude_bucket: str | None = None, rebalance: bool = False) -> dict[str, Any] | None:
    candidates = []
    for row in rows:
        if exclude_bucket is not None and row.get("bucket") == exclude_bucket:
            continue
        ok, _note = is_rebalance_entry(row) if rebalance else is_actionable(row)
        if ok:
            candidates.append(row)
    if not candidates:
        return None
    return sorted(candidates, key=lambda r: (fnum(r.get("edge")) or -999.0), reverse=True)[0]


def make_position(row: dict[str, Any], ts: str, position_id: int, source: str) -> Position:
    ask = best_ask(row) or fnum(row.get("ask"))
    if ask is None:
        raise ValueError("cannot create position without ask")
    book = row_book_details(row, ts)
    bid = best_bid(row)
    bid_size = best_level_size(row, "bid")
    ask_size = best_level_size(row, "ask")
    return Position(
        position_id=position_id,
        event=str(row.get("event")),
        handle=str(row.get("handle")),
        bucket=str(row.get("bucket")),
        question=str(row.get("question")),
        entry_time=ts,
        entry_price=float(ask),
        entry_fair=fnum(row.get("fair")),
        entry_edge=fnum(row.get("edge")),
        entry_count=inum(row.get("count")),
        entry_projected=fnum(row.get("projected")),
        yes_token_id=row.get("yes_token_id"),
        condition_id=row.get("condition_id"),
        source=source,
        entry_bid=bid,
        entry_bid_size=bid_size,
        entry_ask_size=ask_size,
        entry_book_bids=book["bids"],
        entry_book_asks=book["asks"],
        entry_book_request_started_at=book["request_started_at"],
        entry_book_response_received_at=book["response_received_at"],
        entry_book_provider_timestamp=book["provider_timestamp"],
        entry_book_timing_quality=book["timing_quality"],
        latest_bid=bid,
        latest_bid_size=bid_size,
        latest_ask=float(ask),
        latest_ask_size=ask_size,
    )


def close_position(
    pos: Position,
    ts: str,
    exit_price: float,
    reason: str,
    row: dict[str, Any] | None,
    final_count: int | None = None,
) -> ClosedTrade:
    pnl_per_share = exit_price - pos.entry_price
    roi = pnl_per_share / pos.entry_price if pos.entry_price else math.nan
    exit_book = row_book_details(row, ts) if row else None
    exit_bid_size = best_level_size(row, "bid") if row else None
    exact_timing = bool(
        pos.entry_book_timing_quality == "exact_request_response"
        and exit_book and exit_book.get("timing_quality") == "exact_request_response"
    )
    top_feasible = bool(
        exact_timing
        and pos.entry_ask_size is not None and pos.entry_ask_size >= pos.quantity
        and exit_bid_size is not None and exit_bid_size >= pos.quantity
    )
    return ClosedTrade(
        position_id=pos.position_id,
        event=pos.event,
        handle=pos.handle,
        bucket=pos.bucket,
        entry_time=pos.entry_time,
        entry_price=round(pos.entry_price, 4),
        exit_time=ts,
        exit_price=round(exit_price, 4),
        exit_reason=reason,
        pnl_per_share=round(pnl_per_share, 4),
        roi_on_entry=round(roi, 4),
        paper_pnl=round(pnl_per_share * pos.quantity, 2),
        entry_count=pos.entry_count,
        exit_count=inum(row.get("count")) if row else None,
        entry_fair=None if pos.entry_fair is None else round(pos.entry_fair, 4),
        exit_fair=None if not row else fnum(row.get("fair")),
        entry_edge=None if pos.entry_edge is None else round(pos.entry_edge, 4),
        exit_edge=None if not row else fnum(row.get("edge")),
        final_count=final_count,
        won_if_settled=None if final_count is None else bucket_hit(pos.bucket, final_count),
        question=pos.question,
        yes_token_id=pos.yes_token_id,
        condition_id=pos.condition_id,
        source=pos.source,
        quantity=pos.quantity,
        entry_bid=pos.entry_bid,
        entry_bid_size=pos.entry_bid_size,
        entry_ask_size=pos.entry_ask_size,
        entry_book_bids=pos.entry_book_bids,
        entry_book_asks=pos.entry_book_asks,
        entry_book_request_started_at=pos.entry_book_request_started_at,
        entry_book_response_received_at=pos.entry_book_response_received_at,
        entry_book_provider_timestamp=pos.entry_book_provider_timestamp,
        entry_book_timing_quality=pos.entry_book_timing_quality,
        exit_bid_size=exit_bid_size,
        exit_ask=best_ask(row) if row else None,
        exit_ask_size=best_level_size(row, "ask") if row else None,
        exit_book_bids=exit_book.get("bids") if exit_book else None,
        exit_book_asks=exit_book.get("asks") if exit_book else None,
        exit_book_request_started_at=exit_book.get("request_started_at") if exit_book else None,
        exit_book_response_received_at=exit_book.get("response_received_at") if exit_book else None,
        exit_book_provider_timestamp=exit_book.get("provider_timestamp") if exit_book else None,
        exit_book_timing_quality=exit_book.get("timing_quality") if exit_book else None,
        gross_top_of_book_feasible=top_feasible,
        execution_evidence_eligible=top_feasible,
        execution_valid_pnl=False,
        net_capturable=False,
        pnl_validity_reason="legacy_xtracker_sparse_snapshot_top_of_book_zero_fee_no_latency",
    )


def exit_reasons(pos: Position, held_row: dict[str, Any] | None, better_row: dict[str, Any] | None) -> tuple[list[str], float | None]:
    if not held_row:
        return [], None
    bid = best_bid(held_row)
    if bid is None:
        return [], None
    reasons: list[str] = []
    profit = bid - pos.entry_price
    if profit >= MIN_ABSOLUTE_PROFIT_EXIT:
        reasons.append("absolute_profit_exit")
    if profit / pos.entry_price >= MIN_RELATIVE_PROFIT_EXIT:
        reasons.append("relative_profit_exit")
    fair = fnum(held_row.get("fair"))
    if fair is not None and fair <= FAIR_COLLAPSE_THRESHOLD and bid - fair >= STALE_BID_EDGE:
        reasons.append("stale_bucket_bid_above_model")
    if better_row and better_row.get("bucket") != pos.bucket and bid >= pos.entry_price:
        better_edge = fnum(better_row.get("edge")) or 0.0
        held_edge = fnum(held_row.get("edge")) or -999.0
        if better_edge - held_edge >= BETTER_BUCKET_EDGE_DELTA:
            reasons.append("profitable_better_bucket_available")
    return reasons, bid


def simulate_rebalance(records: list[dict[str, Any]], final_counts: dict[str, int]) -> tuple[list[dict[str, Any]], list[ClosedTrade], list[Position]]:
    ledger: list[dict[str, Any]] = []
    closed: list[ClosedTrade] = []
    open_by_event: dict[str, Position] = {}
    completed_events: set[str] = set()
    next_id = 1

    for snapshot in records:
        ts = iso(snapshot["_time"])
        by_event = event_rows(snapshot)
        # First manage existing positions when this snapshot contains their event.
        for ev, rows in by_event.items():
            pos = open_by_event.get(ev)
            if not pos:
                continue
            held_row = next((r for r in rows if r.get("bucket") == pos.bucket), None)
            if held_row:
                pos.latest_bid = best_bid(held_row)
                pos.latest_bid_size = best_level_size(held_row, "bid")
                pos.latest_ask = best_ask(held_row)
                pos.latest_ask_size = best_level_size(held_row, "ask")
            better = best_actionable_row(rows, exclude_bucket=pos.bucket, rebalance=True)
            reasons, bid = exit_reasons(pos, held_row, better)
            if reasons and bid is not None:
                trade = close_position(pos, ts, bid, "+".join(reasons), held_row)
                closed.append(trade)
                ledger.append({"type": "EXIT", **asdict(trade)})
                del open_by_event[ev]
                if better:
                    new_pos = make_position(better, ts, next_id, source="rebalance")
                    next_id += 1
                    open_by_event[ev] = new_pos
                    ledger.append({"type": "ENTRY", **asdict(new_pos)})
                else:
                    completed_events.add(ev)

        # Then open new first-entry positions for events not currently open/done.
        for ev, rows in by_event.items():
            if ev in open_by_event or ev in completed_events:
                continue
            best = best_actionable_row(rows)
            if not best:
                continue
            pos = make_position(best, ts, next_id, source="initial")
            next_id += 1
            open_by_event[ev] = pos
            ledger.append({"type": "ENTRY", **asdict(pos)})

    # Settle any open positions whose final counts are known; leave active windows open.
    for ev, pos in list(open_by_event.items()):
        count = final_counts.get(ev)
        if count is None:
            continue
        won = bucket_hit(pos.bucket, count)
        if won is None:
            continue
        exit_price = 1.0 if won else 0.0
        trade = close_position(pos, "settlement", exit_price, "settlement", None, final_count=count)
        closed.append(trade)
        ledger.append({"type": "SETTLEMENT", **asdict(trade)})
        del open_by_event[ev]
        completed_events.add(ev)

    return ledger, closed, list(open_by_event.values())


def simulate_hold_baseline(records: list[dict[str, Any]], final_counts: dict[str, int]) -> list[ClosedTrade]:
    entries: dict[str, Position] = {}
    next_id = 1
    for snapshot in records:
        ts = iso(snapshot["_time"])
        for ev, rows in event_rows(snapshot).items():
            if ev in entries:
                continue
            best = best_actionable_row(rows)
            if not best:
                continue
            entries[ev] = make_position(best, ts, next_id, source="hold_baseline")
            next_id += 1
    closed: list[ClosedTrade] = []
    for ev, pos in entries.items():
        count = final_counts.get(ev)
        if count is None:
            continue
        won = bucket_hit(pos.bucket, count)
        if won is None:
            continue
        closed.append(close_position(pos, "settlement", 1.0 if won else 0.0, "hold_settlement", None, count))
    return closed


def summarize(trades: list[ClosedTrade]) -> dict[str, Any]:
    if not trades:
        return {
            "closed_trades": 0, "wins": 0, "losses": 0, "breakeven": 0,
            "win_rate": None, "paper_pnl": 0.0, "avg_roi": None,
            "median_roi": None, "median_paper_pnl": None,
            "average_winner": None, "average_loser": None, "profit_factor": None,
            "positive_exits": 0, "negative_exits": 0,
            "gross_top_of_book_feasible_trades": 0,
            "execution_valid_pnl": False, "net_capturable": False,
            "paper_pnl_basis": LEGACY_PNL_VALIDITY["paper_pnl_basis"],
        }
    wins = [t for t in trades if t.pnl_per_share > 0]
    losses = [t for t in trades if t.pnl_per_share < 0]
    breakeven = [t for t in trades if t.pnl_per_share == 0]
    rois = [t.roi_on_entry for t in trades]
    positive_pnl = sum(t.paper_pnl for t in wins)
    negative_pnl = sum(t.paper_pnl for t in losses)
    return {
        "closed_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(breakeven),
        "win_rate": round(len(wins) / len(trades), 4),
        "paper_pnl": round(sum(t.paper_pnl for t in trades), 2),
        "avg_roi": round(sum(rois) / len(rois), 4),
        "median_roi": round(statistics.median(rois), 4),
        "median_paper_pnl": round(statistics.median(t.paper_pnl for t in trades), 4),
        "average_winner": round(positive_pnl / len(wins), 4) if wins else None,
        "average_loser": round(negative_pnl / len(losses), 4) if losses else None,
        "profit_factor": round(positive_pnl / abs(negative_pnl), 4) if negative_pnl < 0 else None,
        "positive_exits": len([t for t in trades if "exit" in t.exit_reason and t.pnl_per_share > 0]),
        "negative_exits": len([t for t in trades if "exit" in t.exit_reason and t.pnl_per_share < 0]),
        "gross_top_of_book_feasible_trades": sum(t.gross_top_of_book_feasible for t in trades),
        "execution_valid_pnl": False,
        "net_capturable": False,
        "paper_pnl_basis": LEGACY_PNL_VALIDITY["paper_pnl_basis"],
    }


def lifecycle_id(event: str) -> str:
    return "xlife_" + hashlib.sha256(event.encode("utf-8", "replace")).hexdigest()[:20]


def reconcile_lifecycles(
    ledger: list[dict[str, Any]], trades: list[ClosedTrade], open_positions: list[Position],
) -> list[dict[str, Any]]:
    """Reconcile cash and inventory per complete market lifecycle."""
    events = sorted({str(row.get("event")) for row in ledger if row.get("event")}
                    | {trade.event for trade in trades} | {pos.event for pos in open_positions})
    output: list[dict[str, Any]] = []
    for event in events:
        entries = [row for row in ledger if row.get("type") == "ENTRY" and row.get("event") == event]
        closed = [trade for trade in trades if trade.event == event]
        opened = [pos for pos in open_positions if pos.event == event]
        initial_cash = 100.0
        buy_notional = sum((fnum(row.get("entry_price")) or 0.0) * (fnum(row.get("quantity")) or STAKE_PER_TRADE) for row in entries)
        sale_notional = sum(trade.exit_price * trade.quantity for trade in closed)
        inventory_value = 0.0
        unrealized_pnl = 0.0
        inventory_basis: list[str] = []
        for pos in opened:
            mark = pos.latest_bid if pos.latest_bid is not None else pos.entry_price
            inventory_value += mark * pos.quantity
            unrealized_pnl += (mark - pos.entry_price) * pos.quantity
            inventory_basis.append("latest_bid" if pos.latest_bid is not None else "entry_price_fallback")
        ending_cash = initial_cash - buy_notional + sale_notional
        realized_pnl = sum(trade.paper_pnl for trade in closed)
        fixed_pnl = ending_cash + inventory_value - initial_cash
        equal_risk_pnl = sum(
            100.0 * (trade.exit_price - trade.entry_price) / trade.entry_price
            for trade in closed if trade.entry_price > 0
        ) + sum(
            100.0 * ((pos.latest_bid if pos.latest_bid is not None else pos.entry_price) - pos.entry_price) / pos.entry_price
            for pos in opened if pos.entry_price > 0
        )
        rebalance_entries = [row for row in entries if row.get("source") == "rebalance"]
        switch_times = {str(row.get("entry_time")) for row in rebalance_entries}
        switch_exit_pnl = sum(trade.paper_pnl for trade in closed if trade.exit_time in switch_times)
        invariant_delta = (ending_cash + inventory_value - initial_cash) - (realized_pnl + unrealized_pnl)
        output.append({
            "market_lifecycle_id": lifecycle_id(event),
            "event": event,
            "handle": (entries[0].get("handle") if entries else closed[0].handle if closed else opened[0].handle),
            "status": "open" if opened else "closed",
            "initial_cash": round(initial_cash, 6),
            "buy_notional": round(buy_notional, 6),
            "sale_or_settlement_proceeds": round(sale_notional, 6),
            "fees_assumed": 0.0,
            "spread_model": "stored entry ask and exit bid; no invented midpoint fills",
            "execution_valid_pnl": False,
            "net_capturable": False,
            "pnl_validity_reason": "legacy_xtracker_sparse_snapshot_top_of_book_zero_fee_no_latency",
            "ending_cash": round(ending_cash, 6),
            "remaining_inventory_shares": round(sum(pos.quantity for pos in opened), 6),
            "inventory_value": round(inventory_value, 6),
            "inventory_valuation_basis": sorted(set(inventory_basis)),
            "realized_pnl": round(realized_pnl, 6),
            "unrealized_pnl": round(unrealized_pnl, 6),
            "fixed_100_share_pnl": round(fixed_pnl, 6),
            "equal_risk_100_dollar_per_leg_pnl": round(equal_risk_pnl, 6),
            "cash_invariant_delta": round(invariant_delta, 10),
            "cash_invariant_reconciled": abs(invariant_delta) <= 1e-8,
            "closed_legs": len(closed),
            "open_legs": len(opened),
            "switches": len(rebalance_entries),
            "switch_entry_notional": round(sum((fnum(row.get("entry_price")) or 0.0) * (fnum(row.get("quantity")) or STAKE_PER_TRADE) for row in rebalance_entries), 6),
            "switch_exit_realized_pnl": round(switch_exit_pnl, 6),
            "gross_top_of_book_feasible_legs": sum(trade.gross_top_of_book_feasible for trade in closed),
            "classification": "win" if fixed_pnl > 0 else "loss" if fixed_pnl < 0 else "breakeven",
        })
    return output


def strategy_signals_from_ledger(ledger: list[dict[str, Any]]) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    for row in ledger:
        row_type = str(row.get("type") or "").upper()
        if row_type not in {"ENTRY", "EXIT"}:
            continue
        prefix = "entry" if row_type == "ENTRY" else "exit"
        ts = row.get(f"{prefix}_time")
        if not ts or ts == "settlement":
            continue
        timing_quality = row.get(f"{prefix}_book_timing_quality")
        book = strategy_evidence.build_book(
            bids=row.get(f"{prefix}_book_bids"),
            asks=row.get(f"{prefix}_book_asks"),
            best_bid=[row.get(f"{prefix}_price" if prefix == "exit" else "entry_bid"), row.get(f"{prefix}_bid_size")],
            best_ask=[row.get(f"{prefix}_ask" if prefix == "exit" else "entry_price"), row.get(f"{prefix}_ask_size")],
            request_started_at=row.get(f"{prefix}_book_request_started_at"),
            response_received_at=row.get(f"{prefix}_book_response_received_at"),
            provider_timestamp=row.get(f"{prefix}_book_provider_timestamp"),
            timing_quality=timing_quality,
            capture_source="xtracker_stored_decision_row",
        )
        mode = "decision_input_order_book" if timing_quality == "exact_request_response" else "historical_replay_unverified"
        signals.append(strategy_evidence.build_decision_signal(
            strategy="xtracker_rebalance",
            strategy_version=FILTER_VERSION,
            action=row_type,
            lifecycle_id=lifecycle_id(str(row.get("event") or "")),
            position_id=row.get("position_id"),
            decision_at=str(ts),
            decision_mode=mode,
            condition_id=row.get("condition_id"),
            token_id=row.get("yes_token_id"),
            outcome="Yes",
            quantity=fnum(row.get("quantity")) or STAKE_PER_TRADE,
            question=row.get("question") or row.get("event"),
            book=book,
            eligible_markout_windows=[],
            primary_horizon="strategy_exit_or_settlement",
            reason=row.get("exit_reason") if row_type == "EXIT" else row.get("source"),
            metadata={
                "event": row.get("event"), "handle": row.get("handle"), "bucket": row.get("bucket"),
                "price": row.get(f"{prefix}_price"), "fair": row.get(f"{prefix}_fair"),
                "edge": row.get(f"{prefix}_edge"), "legacy_paper_pnl": row.get("paper_pnl"),
                "execution_valid_pnl": False,
                "net_capturable": False,
                "pnl_validity_reason": "legacy_xtracker_sparse_snapshot_top_of_book_zero_fee_no_latency",
            },
        ))
    return signals


def by_handle_summary(trades: list[ClosedTrade]) -> list[dict[str, Any]]:
    handles = sorted({t.handle for t in trades})
    out = []
    for h in handles:
        subset = [t for t in trades if t.handle == h]
        row = {"handle": h, **summarize(subset)}
        out.append(row)
    return sorted(out, key=lambda r: r.get("paper_pnl") or 0, reverse=True)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fields: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def make_markdown(payload: dict[str, Any]) -> str:
    rebalance = payload["rebalance_summary"]
    baseline = payload["hold_baseline_summary"]
    lifecycle = payload.get("lifecycle_accounting", {})
    matched_closed = [row for row in payload.get("matched_hold_vs_rebalance", [])
                      if row.get("lifecycle_status") == "closed" and row.get("hold_fixed_100_share_pnl") is not None]
    matched_rebalance_pnl = round(sum(row["rebalance_fixed_100_share_pnl"] for row in matched_closed), 6)
    matched_hold_pnl = round(sum(row["hold_fixed_100_share_pnl"] for row in matched_closed), 6)
    lines = [
        "# xtracker Paper Rebalance Backtest",
        "",
        f"Generated: `{payload['generated_at']}`",
        "",
        "Paper-only. No wallet, no live orders, no X API spend.",
        "Legacy paper PnL is research-only: not execution-valid and not net-capturable because this replay uses sparse stored snapshots, zero-fee legacy accounting, no post-decision fill latency, no depth walking, and no queue model.",
        "",
        "## What changed",
        "",
        "This report compares the old hold-to-resolution approach against an observable rebalance approach that can exit a paper position when snapshots show a profitable/stale bid, then optionally rotate into the strongest current bucket in the same event.",
        "",
        "## Data limitation",
        "",
        f"- Snapshot records replayed: `{payload['snapshot_records']}`",
        "- Snapshots only contain stored watchdog candidate rows, not full historical order books for every bucket.",
        "- Therefore exits/rebalances are conservative: the script only uses bids/asks that were actually stored in snapshots.",
        "",
        "## Summary",
        "",
        "| Strategy | Closed legs | Wins | Losses | Breakeven | Win rate | Paper PnL @100 shares | Median PnL | Profit factor |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        f"| Hold-to-resolution baseline | {baseline['closed_trades']} | {baseline['wins']} | {baseline['losses']} | {baseline['breakeven']} | {baseline['win_rate']} | {baseline['paper_pnl']} | {baseline['median_paper_pnl']} | {baseline['profit_factor']} |",
        f"| Rebalance/exit strategy | {rebalance['closed_trades']} | {rebalance['wins']} | {rebalance['losses']} | {rebalance['breakeven']} | {rebalance['win_rate']} | {rebalance['paper_pnl']} | {rebalance['median_paper_pnl']} | {rebalance['profit_factor']} |",
        "",
        f"- Average winner: `{rebalance['average_winner']}`",
        f"- Average loser: `{rebalance['average_loser']}`",
        f"- The 17th classification is explicit: `{rebalance['breakeven']}` breakeven switch leg.",
        "",
        "## Lifecycle accounting",
        "",
        f"- Market lifecycles: `{lifecycle.get('lifecycles_total')}` total = `{lifecycle.get('closed_lifecycles')}` closed + `{lifecycle.get('open_lifecycles')}` open.",
        f"- Closed lifecycle outcomes: `{lifecycle.get('wins')}` wins + `{lifecycle.get('losses')}` losses + `{lifecycle.get('breakeven')}` breakeven.",
        f"- Cash invariant reconciled for every lifecycle: `{lifecycle.get('all_cash_invariants_reconciled')}`.",
        f"- Switches: `{lifecycle.get('switches')}`; realized PnL on switch exits: `{lifecycle.get('switch_exit_realized_pnl')}`.",
        f"- Fixed 100-share PnL including open bid marks: `{lifecycle.get('fixed_100_share_pnl_including_open_marks')}`.",
        f"- Equal-risk $100 entry-cost per leg PnL including open marks: `{lifecycle.get('equal_risk_100_dollar_per_leg_pnl_including_open_marks')}`.",
        f"- Gross top-of-book feasible completed legs: `{lifecycle.get('gross_top_of_book_feasible_legs')}`.",
        f"- Matched closed opportunities (`{len(matched_closed)}`): rebalance `{matched_rebalance_pnl}` versus hold `{matched_hold_pnl}`.",
        "- Invariant used: `ending_cash + inventory_value - initial_cash = realized_PnL + unrealized_PnL`.",
        "- Legacy fee assumption remains zero and is disclosed; historical request/response timing is absent, so these are not execution-valid or net-capturable results.",
        "",
        f"Open rebalance positions: `{payload['open_positions']}`",
        "",
        "## Rebalance trades by handle",
        "",
        "| Handle | Closed | Wins | Losses | Win rate | Paper PnL | Avg ROI |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in payload["rebalance_by_handle"]:
        lines.append(
            f"| {row['handle']} | {row['closed_trades']} | {row['wins']} | {row['losses']} | {row['win_rate']} | {row['paper_pnl']} | {row['avg_roi']} |"
        )
    lines += [
        "",
        "## Recent/open positions",
        "",
        "| Position | Event | Bucket | Entry | Entry time | Source |",
        "|---:|---|---:|---:|---|---|",
    ]
    for pos in payload["open_position_rows"][:20]:
        lines.append(
            f"| {pos['position_id']} | {pos['event']} | `{pos['bucket']}` | {pos['entry_price']} | {pos['entry_time']} | {pos['source']} |"
        )
    if not payload["open_position_rows"]:
        lines.append("| _None_ | | | | | |")
    lines += [
        "",
        "## Files",
        "",
        f"- Ledger CSV: `{OUT_LEDGER_CSV}`",
        f"- Closed trades CSV: `{OUT_TRADES_CSV}`",
        f"- JSON summary: `{OUT_JSON}`",
        f"- XLSX workbook: `{OUT_XLSX}`",
        f"- Lifecycle CSV: `{OUT_LIFECYCLE_CSV}`",
        f"- Lifecycle JSON: `{OUT_LIFECYCLE_JSON}`",
        f"- Shared-ledger strategy signals: `{STRATEGY_SIGNALS}`",
        "",
        "## Readiness note",
        "",
        "Treat this as research until we see enough closed rebalance trades across multiple windows. A single profitable exit does not prove a durable edge.",
    ]
    return "\n".join(lines) + "\n"


def maybe_write_xlsx(payload: dict[str, Any], ledger_rows: list[dict[str, Any]], trade_rows: list[dict[str, Any]]) -> str:
    try:
        import openpyxl
        from openpyxl import Workbook
    except Exception as exc:  # pragma: no cover - optional dependency path
        return f"xlsx_skipped:{type(exc).__name__}:{exc}"

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    summary_rows = [
        ["metric", "hold_baseline", "rebalance"],
        ["closed_trades", payload["hold_baseline_summary"]["closed_trades"], payload["rebalance_summary"]["closed_trades"]],
        ["wins", payload["hold_baseline_summary"]["wins"], payload["rebalance_summary"]["wins"]],
        ["losses", payload["hold_baseline_summary"]["losses"], payload["rebalance_summary"]["losses"]],
        ["win_rate", payload["hold_baseline_summary"]["win_rate"], payload["rebalance_summary"]["win_rate"]],
        ["paper_pnl", payload["hold_baseline_summary"]["paper_pnl"], payload["rebalance_summary"]["paper_pnl"]],
        ["avg_roi", payload["hold_baseline_summary"]["avg_roi"], payload["rebalance_summary"]["avg_roi"]],
    ]
    for row in summary_rows:
        ws.append(row)

    def add_sheet(name: str, rows: list[dict[str, Any]]) -> None:
        ws2 = wb.create_sheet(name[:31])
        if not rows:
            ws2.append(["none"])
            return
        fields: list[str] = []
        for row in rows:
            for key in row.keys():
                if key not in fields:
                    fields.append(key)
        ws2.append(fields)
        for row in rows:
            ws2.append([
                json.dumps(row.get(key), ensure_ascii=False, sort_keys=True)
                if isinstance(row.get(key), (dict, list, tuple)) else row.get(key)
                for key in fields
            ])

    add_sheet("Ledger", ledger_rows)
    add_sheet("Closed Trades", trade_rows)
    add_sheet("By Handle", payload["rebalance_by_handle"])
    add_sheet("Open Positions", payload["open_position_rows"])
    wb.save(OUT_XLSX)
    return "xlsx_written"


def main() -> int:
    records = load_snapshots()
    final_counts = load_final_counts()
    ledger, rebalance_trades, open_positions = simulate_rebalance(records, final_counts)
    baseline_trades = simulate_hold_baseline(records, final_counts)

    ledger_rows = ledger
    trade_rows = [asdict(t) for t in rebalance_trades]
    baseline_rows = [asdict(t) for t in baseline_trades]
    open_rows = [asdict(p) for p in open_positions]
    lifecycle_rows = reconcile_lifecycles(ledger, rebalance_trades, open_positions)
    baseline_by_event = {trade.event: trade for trade in baseline_trades}
    matched_comparison = [
        {
            "market_lifecycle_id": row["market_lifecycle_id"],
            "event": row["event"],
            "lifecycle_status": row["status"],
            "rebalance_fixed_100_share_pnl": row["fixed_100_share_pnl"],
            "hold_fixed_100_share_pnl": (
                baseline_by_event[row["event"]].paper_pnl if row["event"] in baseline_by_event else None
            ),
            "rebalance_minus_hold": (
                round(row["fixed_100_share_pnl"] - baseline_by_event[row["event"]].paper_pnl, 6)
                if row["event"] in baseline_by_event else None
            ),
        }
        for row in lifecycle_rows
    ]
    signal_result = strategy_evidence.append_signals(STRATEGY_SIGNALS, strategy_signals_from_ledger(ledger))

    write_csv(OUT_LEDGER_CSV, ledger_rows)
    write_csv(OUT_TRADES_CSV, trade_rows)
    write_csv(OUT_LIFECYCLE_CSV, lifecycle_rows)
    OUT_LIFECYCLE_JSON.write_text(json.dumps({
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "accounting_basis": "per-market lifecycle; $100 initial cash; 100 paper shares per leg; zero fees retained as legacy assumption",
        "execution_validity": LEGACY_PNL_VALIDITY,
        "lifecycles": lifecycle_rows,
        "matched_hold_vs_rebalance": matched_comparison,
    }, indent=2, ensure_ascii=False))

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "paper_only": True,
        "execution_validity": LEGACY_PNL_VALIDITY,
        "snapshot_records": len(records),
        "source_snapshots": str(SNAPSHOTS),
        "source_proof": str(PROOF),
        "rules": {
            "entry": {
                "filter_version": FILTER_VERSION,
                "min_edge": MIN_EDGE,
                "min_fair": MIN_FAIR,
                "max_ask": MAX_ASK,
                "max_entry_remaining_hours": MAX_ENTRY_REMAINING_HOURS,
                "early_low_bucket_remaining_hours": EARLY_LOW_BUCKET_REMAINING_HOURS,
                "min_qty": MIN_QTY,
                "min_cost_low": MIN_COST_LOW,
                "min_cost_normal": MIN_COST_NORMAL,
            },
            "exit": {
                "min_absolute_profit_exit": MIN_ABSOLUTE_PROFIT_EXIT,
                "min_relative_profit_exit": MIN_RELATIVE_PROFIT_EXIT,
                "fair_collapse_threshold": FAIR_COLLAPSE_THRESHOLD,
                "stale_bid_edge": STALE_BID_EDGE,
                "better_bucket_edge_delta": BETTER_BUCKET_EDGE_DELTA,
                "rebalance_min_edge": REBALANCE_MIN_EDGE,
                "rebalance_min_fair": REBALANCE_MIN_FAIR,
                "rebalance_max_ask": REBALANCE_MAX_ASK,
                "bucket_switch_requires_bid_at_or_above_entry": True,
            },
            "stake_per_trade_shares": STAKE_PER_TRADE,
        },
        "hold_baseline_summary": summarize(baseline_trades),
        "rebalance_summary": summarize(rebalance_trades),
        "rebalance_by_handle": by_handle_summary(rebalance_trades),
        "open_positions": len(open_positions),
        "open_position_rows": open_rows,
        "closed_trade_rows": trade_rows,
        "hold_baseline_trade_rows": baseline_rows,
        "lifecycle_accounting": {
            "lifecycles_total": len(lifecycle_rows),
            "closed_lifecycles": sum(row["status"] == "closed" for row in lifecycle_rows),
            "open_lifecycles": sum(row["status"] == "open" for row in lifecycle_rows),
            "wins": sum(row["status"] == "closed" and row["classification"] == "win" for row in lifecycle_rows),
            "losses": sum(row["status"] == "closed" and row["classification"] == "loss" for row in lifecycle_rows),
            "breakeven": sum(row["status"] == "closed" and row["classification"] == "breakeven" for row in lifecycle_rows),
            "all_cash_invariants_reconciled": all(row["cash_invariant_reconciled"] for row in lifecycle_rows),
            "switches": sum(row["switches"] for row in lifecycle_rows),
            "switch_exit_realized_pnl": round(sum(row["switch_exit_realized_pnl"] for row in lifecycle_rows), 6),
            "fixed_100_share_pnl_including_open_marks": round(sum(row["fixed_100_share_pnl"] for row in lifecycle_rows), 6),
            "equal_risk_100_dollar_per_leg_pnl_including_open_marks": round(sum(row["equal_risk_100_dollar_per_leg_pnl"] for row in lifecycle_rows), 6),
            "gross_top_of_book_feasible_legs": sum(row["gross_top_of_book_feasible_legs"] for row in lifecycle_rows),
        },
        "lifecycle_rows": lifecycle_rows,
        "matched_hold_vs_rebalance": matched_comparison,
        "strategy_signal_ingestion": signal_result,
        "files": {
            "ledger_csv": str(OUT_LEDGER_CSV),
            "trades_csv": str(OUT_TRADES_CSV),
            "summary_md": str(OUT_SUMMARY_MD),
            "summary_json": str(OUT_JSON),
            "xlsx": str(OUT_XLSX),
            "lifecycle_csv": str(OUT_LIFECYCLE_CSV),
            "lifecycle_json": str(OUT_LIFECYCLE_JSON),
            "strategy_signals": str(STRATEGY_SIGNALS),
        },
    }
    xlsx_status = maybe_write_xlsx(payload, ledger_rows, trade_rows)
    payload["xlsx_status"] = xlsx_status
    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    OUT_SUMMARY_MD.write_text(make_markdown(payload))

    print(json.dumps({
        "summary_md": str(OUT_SUMMARY_MD),
        "summary_json": str(OUT_JSON),
        "ledger_csv": str(OUT_LEDGER_CSV),
        "trades_csv": str(OUT_TRADES_CSV),
        "xlsx": str(OUT_XLSX),
        "xlsx_status": xlsx_status,
        "snapshot_records": len(records),
        "hold_baseline_summary": payload["hold_baseline_summary"],
        "rebalance_summary": payload["rebalance_summary"],
        "open_positions": len(open_positions),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
