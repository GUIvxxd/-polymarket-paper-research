#!/usr/bin/env python3
"""Polymarket stock/price-market paper bot.

This is the stock-price equivalent of the xtracker tweet-count paper loop:
- discover clean price-threshold Polymarket markets;
- compute public-data fair value from reference prices + time/volatility;
- store recurring snapshots;
- create paper entries only when the scanner marks a row actionable;
- mark-to-market/settle open paper positions;
- write audit reports and stay quiet unless something changes.

Safety boundary: public data only, no wallet, no private key, no live orders.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import multi_market_research_bot as mm
import strategy_evidence

ROOT = Path("/data/workspace/polymarket-research")
REPORTS = ROOT / "reports"
DATA = ROOT / "data"
STATE = DATA / "stock_price_paper_state.json"
SNAPSHOT_LOG = REPORTS / "stock_price_snapshots.jsonl"
SUMMARY_JSON = REPORTS / "stock_price_paper_summary_latest.json"
SUMMARY_MD = REPORTS / "stock_price_paper_summary_latest.md"
LEDGER_CSV = REPORTS / "stock_price_paper_ledger_latest.csv"
TRADES_CSV = REPORTS / "stock_price_paper_trades_latest.csv"
STRATEGY_SIGNALS = REPORTS / "stock_price_strategy_decisions.jsonl"

FILTER_VERSION = "stock_price_consensus_v2_2026_07_16"
STAKE_SHARES = 100.0
MIN_ABSOLUTE_PROFIT_EXIT = 0.03
MIN_RELATIVE_PROFIT_EXIT = 0.15
FAIR_COLLAPSE_THRESHOLD = 0.20
STALE_BID_OVER_FAIR = 0.12
CONSENSUS_MIN_SIGNALS = 9
MAX_ENTRY_SPREAD = 0.06
MIN_EDGE_TO_SPREAD = 1.25
MIN_REFERENCE_DISTANCE = 0.005
LEGACY_PNL_VALIDITY = {
    "execution_valid_pnl": False,
    "net_capturable": False,
    "paper_pnl_basis": "legacy_stock_top_of_book_100_shares",
    "invalid_reasons": [
        "no_polymarket_fee_accounting",
        "no_post_decision_latency_fill_book",
        "no_depth_walk_or_queue_model",
        "settlement_from_public_reference_price_not_polymarket_resolution_proof",
    ],
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def fnum(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def candidate_to_dict(candidate: Any) -> dict[str, Any]:
    if is_dataclass(candidate):
        return asdict(candidate)
    return dict(candidate)


def candidate_key(row: dict[str, Any]) -> str:
    return "|".join(
        str(row.get(key) or "")
        for key in ("condition_id", "side", "outcome", "ticker", "threshold", "direction")
    )


def market_key(row: dict[str, Any]) -> str:
    return str(row.get("condition_id") or row.get("slug") or row.get("question") or candidate_key(row))


def entry_consensus_result(row: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """Score independent checks before opening a stock/price paper entry."""
    edge = fnum(row.get("edge"))
    ask = fnum(row.get("ask"))
    bid = fnum(row.get("bid"))
    fair = fnum(row.get("fair_probability"))
    ask_size = fnum(row.get("ask_size"))
    seconds_to_end = fnum(row.get("seconds_to_end"))
    current_price = fnum(row.get("current_price"))
    threshold = fnum(row.get("threshold"))
    spread = ask - bid if ask is not None and bid is not None else None
    edge_to_spread = edge / spread if edge is not None and spread and spread > 0 else None
    reference_distance = None
    if current_price is not None and threshold is not None and current_price != 0:
        reference_distance = abs(current_price - threshold) / abs(current_price)

    signals: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: str) -> None:
        signals.append({"name": name, "pass": bool(passed), "detail": detail})

    add("scanner_actionable", bool(row.get("actionable")), f"actionable={row.get('actionable')};note={row.get('note')}")
    add("model_inputs_present", edge is not None and fair is not None and ask is not None, f"edge={edge};fair={fair};ask={ask}")
    add("edge_above_min", edge is not None and edge >= args.min_edge, f"edge={edge};min={args.min_edge}")
    add("fair_above_min", fair is not None and fair >= args.min_fair, f"fair={fair};min={args.min_fair}")
    add("ask_below_max", ask is not None and ask <= args.max_ask, f"ask={ask};max={args.max_ask}")
    add("liquidity_size_ok", ask_size is not None and ask_size >= args.min_size, f"ask_size={ask_size};min={args.min_size}")
    add("time_to_end_ok", seconds_to_end is None or seconds_to_end >= args.min_seconds_to_end, f"seconds_to_end={seconds_to_end};min={args.min_seconds_to_end}")
    add("live_bid_present", bid is not None, f"bid={bid}")
    add("spread_ok", spread is not None and spread <= args.max_entry_spread, f"spread={spread};max={args.max_entry_spread}")
    add("edge_covers_spread", edge_to_spread is not None and edge_to_spread >= args.min_edge_to_spread, f"edge_to_spread={edge_to_spread};min={args.min_edge_to_spread}")
    add("reference_price_present", current_price is not None and bool(row.get("price_source")), f"current_price={current_price};source={row.get('price_source')}")
    add("reference_distance_ok", reference_distance is not None and reference_distance >= args.min_reference_distance, f"distance={reference_distance};min={args.min_reference_distance}")

    passed = sum(1 for signal in signals if signal["pass"])
    failed = [signal["name"] for signal in signals if not signal["pass"]]
    return {
        "version": FILTER_VERSION,
        "passed": passed,
        "required": args.min_consensus,
        "total": len(signals),
        "failed": failed,
        "signals": signals,
    }


def entry_consensus_note(result: dict[str, Any]) -> str:
    failed = result.get("failed") or []
    failed_text = ",".join(failed[:5]) if failed else "none"
    return f"consensus={result.get('passed')}/{result.get('total')} required={result.get('required')} failed={failed_text}"


def is_entry_candidate(row: dict[str, Any], args: argparse.Namespace) -> tuple[bool, str]:
    consensus = entry_consensus_result(row, args)
    note = entry_consensus_note(consensus)
    if not row.get("actionable"):
        return False, f"scanner_not_actionable;{note}"
    edge = fnum(row.get("edge"))
    ask = fnum(row.get("ask"))
    fair = fnum(row.get("fair_probability"))
    ask_size = fnum(row.get("ask_size"))
    seconds_to_end = fnum(row.get("seconds_to_end"))
    if edge is None or ask is None or fair is None:
        return False, f"missing_edge_ask_or_fair;{note}"
    if ask_size is None or ask_size < args.min_size:
        return False, f"ask_size_below_{args.min_size};{note}"
    if edge < args.min_edge:
        return False, f"edge_below_{args.min_edge};{note}"
    if ask > args.max_ask:
        return False, f"ask_above_{args.max_ask};{note}"
    if fair < args.min_fair:
        return False, f"fair_below_{args.min_fair};{note}"
    if seconds_to_end is not None and seconds_to_end <= 0:
        return False, f"market_already_past_end;{note}"
    if seconds_to_end is not None and seconds_to_end < args.min_seconds_to_end:
        return False, f"too_close_to_end_under_{args.min_seconds_to_end}s;{note}"
    if int(consensus.get("passed") or 0) < args.min_consensus:
        return False, f"consensus_below_threshold;{note}"
    return True, f"filter={FILTER_VERSION};edge={edge:.4f};fair={fair:.4f};ask={ask:.4f};size={ask_size:.2f};{note}"


def open_position(row: dict[str, Any], run_at: str, next_id: int, reason: str) -> dict[str, Any]:
    entry_price = fnum(row.get("ask")) or 0.0
    return {
        "id": next_id,
        "status": "open",
        "key": candidate_key(row),
        "market_key": market_key(row),
        "condition_id": row.get("condition_id"),
        "market_id": row.get("market_id"),
        "token_id": row.get("token_id"),
        "slug": row.get("slug"),
        "question": row.get("question"),
        "ticker": row.get("ticker"),
        "side": row.get("side"),
        "outcome": row.get("outcome"),
        "threshold": row.get("threshold"),
        "direction": row.get("direction"),
        "end_date": row.get("end_date"),
        "entry_time": run_at,
        "entry_price": entry_price,
        "entry_bid": row.get("bid"),
        "entry_bid_size": row.get("bid_size"),
        "entry_ask_size": row.get("ask_size"),
        "entry_book_bids": row.get("book_bids"),
        "entry_book_asks": row.get("book_asks"),
        "entry_book_request_started_at": row.get("book_request_started_at"),
        "entry_book_response_received_at": row.get("book_response_received_at"),
        "entry_book_provider_timestamp": row.get("book_provider_timestamp"),
        "entry_book_timing_quality": row.get("book_timing_quality"),
        "entry_fair": row.get("fair_probability"),
        "entry_edge": row.get("edge"),
        "entry_reference_price": row.get("current_price"),
        "entry_price_source": row.get("price_source"),
        "entry_reason": reason,
        "stake_shares": STAKE_SHARES,
        "latest_time": run_at,
        "latest_bid": row.get("bid"),
        "latest_bid_size": row.get("bid_size"),
        "latest_ask": row.get("ask"),
        "latest_ask_size": row.get("ask_size"),
        "latest_fair": row.get("fair_probability"),
        "latest_edge": row.get("edge"),
        "latest_reference_price": row.get("current_price"),
        "latest_note": row.get("note"),
    }


def close_position(pos: dict[str, Any], *, run_at: str, exit_price: float, reason: str, row: dict[str, Any] | None = None) -> dict[str, Any]:
    entry = fnum(pos.get("entry_price")) or 0.0
    pnl = (exit_price - entry) * STAKE_SHARES
    cost = entry * STAKE_SHARES
    roi = pnl / cost if cost > 0 else None
    pos["status"] = "closed"
    pos["exit_time"] = run_at
    pos["exit_price"] = round(exit_price, 6)
    pos["exit_reason"] = reason
    pos["paper_pnl"] = round(pnl, 4)
    pos["roi_on_entry"] = round(roi, 6) if roi is not None else None
    if row is not None:
        pos["latest_bid"] = row.get("bid")
        pos["latest_bid_size"] = row.get("bid_size")
        pos["latest_ask"] = row.get("ask")
        pos["latest_ask_size"] = row.get("ask_size")
        pos["latest_fair"] = row.get("fair_probability")
        pos["latest_edge"] = row.get("edge")
        pos["latest_reference_price"] = row.get("current_price")
        pos["latest_note"] = row.get("note")
        pos["exit_bid_size"] = row.get("bid_size")
        pos["exit_ask"] = row.get("ask")
        pos["exit_ask_size"] = row.get("ask_size")
        pos["exit_book_bids"] = row.get("book_bids")
        pos["exit_book_asks"] = row.get("book_asks")
        pos["exit_book_request_started_at"] = row.get("book_request_started_at")
        pos["exit_book_response_received_at"] = row.get("book_response_received_at")
        pos["exit_book_provider_timestamp"] = row.get("book_provider_timestamp")
        pos["exit_book_timing_quality"] = row.get("book_timing_quality")
    exact = bool(
        pos.get("entry_book_timing_quality") == "exact_request_response"
        and pos.get("exit_book_timing_quality") == "exact_request_response"
    )
    pos["gross_top_of_book_feasible"] = bool(
        exact
        and (fnum(pos.get("entry_ask_size")) or 0) >= (fnum(pos.get("stake_shares")) or STAKE_SHARES)
        and (fnum(pos.get("exit_bid_size")) or 0) >= (fnum(pos.get("stake_shares")) or STAKE_SHARES)
    )
    pos["execution_evidence_eligible"] = pos["gross_top_of_book_feasible"]
    pos["execution_valid_pnl"] = False
    pos["net_capturable"] = False
    pos["pnl_validity_reason"] = "legacy_stock_top_of_book_no_fees_no_latency_no_resolution_proof"
    return pos


def settle_price_wins(direction: str | None, reference_price: float, threshold: float) -> bool:
    if direction == "below":
        return reference_price <= threshold
    return reference_price >= threshold


def settlement_exit_price(pos: dict[str, Any], reference_price: float) -> tuple[float, str]:
    threshold = fnum(pos.get("threshold"))
    if threshold is None:
        return 0.0, "settlement_failed_missing_threshold"
    yes_wins = settle_price_wins(str(pos.get("direction") or "above"), reference_price, threshold)
    side = str(pos.get("side") or "YES").upper()
    wins = yes_wins if side == "YES" else not yes_wins
    return (1.0 if wins else 0.0), f"settled_from_public_price;reference={reference_price};threshold={threshold};yes_wins={yes_wins}"


def try_reference_settlement(pos: dict[str, Any]) -> tuple[float, str] | None:
    ticker = str(pos.get("ticker") or "")
    spec = next((s for s in mm.TICKERS if s.symbol == ticker), None)
    if spec is None:
        return None
    try:
        price = mm.fetch_external_price(spec)
    except Exception:
        return None
    return settlement_exit_price(pos, price.price)


def update_open_position(pos: dict[str, Any], row: dict[str, Any] | None, run_at: str, args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    if row is not None:
        pos["latest_time"] = run_at
        pos["latest_bid"] = row.get("bid")
        pos["latest_bid_size"] = row.get("bid_size")
        pos["latest_ask"] = row.get("ask")
        pos["latest_ask_size"] = row.get("ask_size")
        pos["latest_fair"] = row.get("fair_probability")
        pos["latest_edge"] = row.get("edge")
        pos["latest_reference_price"] = row.get("current_price")
        pos["latest_note"] = row.get("note")

        seconds_to_end = fnum(row.get("seconds_to_end"))
        reference_price = fnum(row.get("current_price"))
        if seconds_to_end is not None and seconds_to_end <= 0 and reference_price is not None:
            exit_price, reason = settlement_exit_price(pos, reference_price)
            close_position(pos, run_at=run_at, exit_price=exit_price, reason=reason, row=row)
            return pos, f"SETTLED position {pos['id']}: {pos.get('ticker')} {pos.get('side')} exit={exit_price:.2f} pnl={pos.get('paper_pnl')}"

        bid = fnum(row.get("bid"))
        fair = fnum(row.get("fair_probability"))
        entry = fnum(pos.get("entry_price")) or 0.0
        if bid is None:
            return pos, None
        profit_abs = bid - entry
        profit_rel = profit_abs / entry if entry > 0 else 0.0
        if profit_abs >= args.min_profit_exit and profit_rel >= args.min_roi_exit:
            close_position(pos, run_at=run_at, exit_price=bid, reason="profit_take_bid", row=row)
            return pos, f"EXIT position {pos['id']}: {pos.get('ticker')} {pos.get('side')} profit_take bid={bid:.4f} pnl={pos.get('paper_pnl')}"
        if fair is not None and fair <= args.fair_collapse and bid >= entry and (bid - fair) >= args.stale_bid_over_fair:
            close_position(pos, run_at=run_at, exit_price=bid, reason="stale_bid_after_fair_collapse", row=row)
            return pos, f"EXIT position {pos['id']}: {pos.get('ticker')} {pos.get('side')} stale_bid bid={bid:.4f} fair={fair:.4f} pnl={pos.get('paper_pnl')}"
        return pos, None

    end = parse_dt(pos.get("end_date"))
    if end is not None and datetime.now(UTC) >= end:
        settlement = try_reference_settlement(pos)
        if settlement is not None:
            exit_price, reason = settlement
            close_position(pos, run_at=run_at, exit_price=exit_price, reason=reason, row=None)
            return pos, f"SETTLED position {pos['id']}: {pos.get('ticker')} {pos.get('side')} exit={exit_price:.2f} pnl={pos.get('paper_pnl')}"
    return pos, None


def stock_lifecycle_id(position: dict[str, Any]) -> str:
    key = str(position.get("market_key") or position.get("condition_id") or position.get("question") or position.get("id"))
    return "stocklife_" + hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:20]


def strategy_signals_from_positions(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    for pos in positions:
        for action, prefix in (("ENTRY", "entry"), ("EXIT", "exit")):
            timestamp = pos.get(f"{prefix}_time")
            if not timestamp:
                continue
            timing_quality = pos.get(f"{prefix}_book_timing_quality")
            book = strategy_evidence.build_book(
                bids=pos.get(f"{prefix}_book_bids"),
                asks=pos.get(f"{prefix}_book_asks"),
                best_bid=[pos.get(f"{prefix}_price" if prefix == "exit" else "entry_bid"), pos.get(f"{prefix}_bid_size")],
                best_ask=[pos.get(f"{prefix}_ask" if prefix == "exit" else "entry_price"), pos.get(f"{prefix}_ask_size")],
                request_started_at=pos.get(f"{prefix}_book_request_started_at"),
                response_received_at=pos.get(f"{prefix}_book_response_received_at"),
                provider_timestamp=pos.get(f"{prefix}_book_provider_timestamp"),
                timing_quality=timing_quality,
                capture_source="stock_price_strategy_decision",
            )
            mode = "decision_input_order_book" if timing_quality == "exact_request_response" else "historical_replay_unverified"
            signals.append(strategy_evidence.build_decision_signal(
                strategy="stock_price_polymarket",
                strategy_version=FILTER_VERSION,
                action=action,
                lifecycle_id=stock_lifecycle_id(pos),
                position_id=pos.get("id"),
                decision_at=str(timestamp),
                decision_mode=mode,
                condition_id=pos.get("condition_id"),
                market_id=pos.get("market_id"),
                slug=pos.get("slug"),
                token_id=pos.get("token_id"),
                outcome=pos.get("outcome"),
                quantity=fnum(pos.get("stake_shares")) or STAKE_SHARES,
                question=pos.get("question"),
                book=book,
                eligible_markout_windows=[],
                primary_horizon="strategy_exit_or_settlement",
                reason=pos.get("entry_reason") if action == "ENTRY" else pos.get("exit_reason"),
                metadata={
                    "ticker": pos.get("ticker"), "side": pos.get("side"),
                    "price": pos.get(f"{prefix}_price"), "legacy_paper_pnl": pos.get("paper_pnl"),
                    "gross_top_of_book_feasible": pos.get("gross_top_of_book_feasible"),
                    "execution_valid_pnl": False,
                    "net_capturable": False,
                    "pnl_validity_reason": "legacy_stock_top_of_book_no_fees_no_latency_no_resolution_proof",
                },
            ))
    return signals


def summarize_positions(positions: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [p for p in positions if p.get("status") == "closed"]
    open_positions = [p for p in positions if p.get("status") == "open"]
    wins = sum(1 for p in closed if (fnum(p.get("paper_pnl")) or 0) > 0)
    losses = sum(1 for p in closed if (fnum(p.get("paper_pnl")) or 0) < 0)
    breakeven = sum(1 for p in closed if (fnum(p.get("paper_pnl")) or 0) == 0)
    pnl = sum(fnum(p.get("paper_pnl")) or 0.0 for p in closed)
    rois = [fnum(p.get("roi_on_entry")) for p in closed if fnum(p.get("roi_on_entry")) is not None]
    return {
        "positions_total": len(positions),
        "open_positions": len(open_positions),
        "closed_trades": len(closed),
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "win_rate": round(wins / len(closed), 4) if closed else None,
        "paper_pnl_at_100_shares": round(pnl, 4),
        "execution_valid_pnl": False,
        "net_capturable": False,
        "paper_pnl_basis": LEGACY_PNL_VALIDITY["paper_pnl_basis"],
        "avg_roi": round(sum(rois) / len(rois), 6) if rois else None,
        "gross_top_of_book_feasible_trades": sum(bool(p.get("gross_top_of_book_feasible")) for p in closed),
        "execution_evidence_eligible_trades": sum(bool(p.get("execution_evidence_eligible")) for p in closed),
    }


def count_lines(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8") as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines: list[str] = []
    lines.append("# Stock/price Polymarket paper bot")
    lines.append("")
    lines.append(f"Generated: `{report['run_at_utc']}`")
    lines.append("")
    lines.append("Paper-only. No wallet, no private keys, no live orders.")
    lines.append("Legacy paper PnL is research-only: not execution-valid and not net-capturable because fees, post-decision fill latency, depth walking, queueing, and Polymarket resolution proof are not fully modeled here.")
    lines.append("")
    lines.append("## Scanner run")
    lines.append("")
    stats = report.get("scan_stats", {})
    lines.append(f"- Markets scanned: `{stats.get('markets_seen', 0)}`")
    lines.append(f"- Stock-like markets: `{stats.get('stock_like_markets', 0)}`")
    lines.append(f"- Clean modeled markets: `{stats.get('modeled_markets', 0)}`")
    lines.append(f"- Modeled YES/NO rows stored this run: `{report.get('snapshot_rows_written', 0)}`")
    lines.append(f"- Total snapshot rows: `{report.get('snapshot_rows_total', 0)}`")
    lines.append(f"- Actionable rows this run: `{stats.get('actionable_candidates', 0)}`")
    params = report.get("parameters", {})
    lines.append(f"- Entry consensus gate: `min {params.get('min_consensus')} / 12 signals`, max spread `{params.get('max_entry_spread')}`, edge/spread min `{params.get('min_edge_to_spread')}`")
    lines.append("")
    lines.append("## Paper ledger")
    lines.append("")
    lines.append("| Closed | Wins | Losses | Breakeven | Win rate | Open | Paper PnL @100 shares | Avg ROI |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|")
    lines.append(
        "| "
        f"{summary['closed_trades']} | {summary['wins']} | {summary['losses']} | {summary['breakeven']} | "
        f"{_fmt(summary['win_rate'])} | {summary['open_positions']} | {summary['paper_pnl_at_100_shares']} | {_fmt(summary['avg_roi'])} |"
    )
    lines.append("")
    if report.get("events"):
        lines.append("## New events")
        lines.append("")
        for event in report["events"]:
            lines.append(f"- {event}")
        lines.append("")
    lines.append("## Open positions")
    lines.append("")
    open_positions = [p for p in report.get("positions", []) if p.get("status") == "open"]
    if open_positions:
        lines.append("| ID | Ticker | Side | Entry | Latest bid | Latest fair | Question |")
        lines.append("|---:|---|---|---:|---:|---:|---|")
        for p in open_positions:
            lines.append(
                "| "
                f"{p.get('id')} | {p.get('ticker')} | {p.get('side')} | {_fmt(p.get('entry_price'))} | "
                f"{_fmt(p.get('latest_bid'))} | {_fmt(p.get('latest_fair'))} | {_md_short(p.get('question'), 90)} |"
            )
    else:
        lines.append("No open paper positions.")
    lines.append("")
    lines.append("## Top watchlist rows")
    lines.append("")
    lines.append("| Ticker | Side | Question | Fair | Ask | Edge | Note |")
    lines.append("|---|---|---|---:|---:|---:|---|")
    for row in report.get("top_watchlist", [])[:12]:
        lines.append(
            "| "
            f"{row.get('ticker')} | {row.get('side')} | {_md_short(row.get('question'), 80)} | "
            f"{_fmt(row.get('fair_probability'))} | {_fmt(row.get('ask'))} | {_fmt(row.get('edge'))} | {_md_short(row.get('note'), 80)} |"
        )
    lines.append("")
    lines.append("## Files")
    lines.append("")
    lines.append(f"- Snapshot JSONL: `{SNAPSHOT_LOG}`")
    lines.append(f"- State JSON: `{STATE}`")
    lines.append(f"- Ledger CSV: `{LEDGER_CSV}`")
    lines.append(f"- Closed trades CSV: `{TRADES_CSV}`")
    lines.append(f"- Summary JSON: `{SUMMARY_JSON}`")
    lines.append("")
    lines.append("## Readiness note")
    lines.append("")
    lines.append("This mirrors the tweet bot pattern, but it is still research mode. Wait for repeated actionable entries, realistic exits, and resolved settlements before treating the module as live-ready.")
    lines.append("")
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    number = fnum(value)
    if number is None:
        return ""
    return f"{number:.4f}".rstrip("0").rstrip(".")


def _md_short(value: Any, n: int) -> str:
    text = "" if value is None else str(value).replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def run_scan(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    markets = mm.collect_market_pool(args.max_markets, args.search_limit)
    candidates, stats = mm.scan_stock_markets(
        markets,
        max_books=args.max_books,
        min_edge=args.min_edge,
        min_size=args.min_size,
        max_ask=args.max_ask,
        include_all_sides=True,
    )
    rows = [candidate_to_dict(c) for c in candidates]
    return rows, stats


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    run_at = utc_now()
    rows, stats = run_scan(args)
    snapshot_rows = [{"run_at_utc": run_at, "filter_version": FILTER_VERSION, **row} for row in rows]
    append_jsonl(SNAPSHOT_LOG, snapshot_rows)

    state = load_json(STATE, {"positions": [], "seen_entry_keys": [], "next_position_id": 1})
    positions: list[dict[str, Any]] = list(state.get("positions") or [])
    seen_entry_keys = set(state.get("seen_entry_keys") or [])
    next_position_id = int(state.get("next_position_id") or 1)
    by_key = {candidate_key(row): row for row in rows}
    open_market_keys = {p.get("market_key") for p in positions if p.get("status") == "open"}
    events: list[str] = []

    for pos in positions:
        if pos.get("status") != "open":
            continue
        decision_at = utc_now()
        updated, event = update_open_position(pos, by_key.get(str(pos.get("key"))), decision_at, args)
        pos.update(updated)
        if event:
            events.append(event)

    open_market_keys = {p.get("market_key") for p in positions if p.get("status") == "open"}
    for row in rows:
        ok, reason = is_entry_candidate(row, args)
        key = candidate_key(row)
        mkey = market_key(row)
        if not ok:
            continue
        if key in seen_entry_keys and not args.allow_reentry:
            continue
        if mkey in open_market_keys:
            continue
        decision_at = utc_now()
        pos = open_position(row, decision_at, next_position_id, reason)
        positions.append(pos)
        seen_entry_keys.add(key)
        open_market_keys.add(mkey)
        events.append(
            f"ENTRY position {next_position_id}: {row.get('ticker')} {row.get('side')} ask={_fmt(row.get('ask'))} edge={_fmt(row.get('edge'))} | {row.get('question')}"
        )
        next_position_id += 1

    state_out = {
        "last_run_at": run_at,
        "filter_version": FILTER_VERSION,
        "next_position_id": next_position_id,
        "seen_entry_keys": sorted(seen_entry_keys),
        "positions": positions,
        "latest_scan_stats": stats,
        "latest_snapshot_log": str(SNAPSHOT_LOG),
        "latest_summary": str(SUMMARY_JSON),
    }
    save_json(STATE, state_out)
    strategy_signal_result = strategy_evidence.append_signals(
        STRATEGY_SIGNALS, strategy_signals_from_positions(positions)
    )

    summary = summarize_positions(positions)
    top_watchlist = sorted(
        [r for r in rows if not r.get("actionable")],
        key=lambda r: fnum(r.get("edge")) if fnum(r.get("edge")) is not None else -999,
        reverse=True,
    )[:20]
    report = {
        "run_at_utc": run_at,
        "paper_only": True,
        "execution_validity": LEGACY_PNL_VALIDITY,
        "filter_version": FILTER_VERSION,
        "parameters": {
            "max_markets": args.max_markets,
            "search_limit": args.search_limit,
            "max_books": args.max_books,
            "min_edge": args.min_edge,
            "min_fair": args.min_fair,
            "min_size": args.min_size,
            "max_ask": args.max_ask,
            "min_profit_exit": args.min_profit_exit,
            "min_roi_exit": args.min_roi_exit,
            "min_consensus": args.min_consensus,
            "max_entry_spread": args.max_entry_spread,
            "min_edge_to_spread": args.min_edge_to_spread,
            "min_reference_distance": args.min_reference_distance,
        },
        "scan_stats": stats,
        "snapshot_rows_written": len(snapshot_rows),
        "snapshot_rows_total": count_lines(SNAPSHOT_LOG),
        "summary": summary,
        "strategy_signal_ingestion": strategy_signal_result,
        "strategy_signal_path": str(STRATEGY_SIGNALS),
        "events": events,
        "positions": positions,
        "top_watchlist": top_watchlist,
    }
    save_json(SUMMARY_JSON, report)
    SUMMARY_MD.write_text(render_markdown(report), encoding="utf-8")
    fields = [
        "id",
        "status",
        "ticker",
        "side",
        "question",
        "entry_time",
        "entry_price",
        "entry_fair",
        "entry_edge",
        "entry_reason",
        "latest_time",
        "latest_bid",
        "latest_ask",
        "latest_fair",
        "latest_edge",
        "exit_time",
        "exit_price",
        "exit_reason",
        "paper_pnl",
        "roi_on_entry",
        "execution_valid_pnl",
        "net_capturable",
        "pnl_validity_reason",
    ]
    write_csv(LEDGER_CSV, positions, fields)
    write_csv(TRADES_CSV, [p for p in positions if p.get("status") == "closed"], fields)
    return report


def run_self_tests() -> None:
    assert settle_price_wins("above", 101, 100)
    assert not settle_price_wins("above", 99, 100)
    assert settle_price_wins("below", 99, 100)
    assert not settle_price_wins("below", 101, 100)
    pos_yes = {"direction": "above", "threshold": 100, "side": "YES", "entry_price": 0.4}
    pos_no = {"direction": "above", "threshold": 100, "side": "NO", "entry_price": 0.4}
    assert settlement_exit_price(pos_yes, 101)[0] == 1.0
    assert settlement_exit_price(pos_no, 101)[0] == 0.0
    fake = {"condition_id": "abc", "side": "YES", "outcome": "Yes", "ticker": "SPY", "threshold": 500, "direction": "above"}
    assert candidate_key(fake).startswith("abc|YES|Yes|SPY")

    args = build_parser().parse_args(["--min-consensus", "9"])
    strong = {
        "actionable": True,
        "edge": 0.12,
        "ask": 0.30,
        "bid": 0.28,
        "fair_probability": 0.42,
        "ask_size": 25,
        "seconds_to_end": 3600,
        "current_price": 100,
        "threshold": 110,
        "price_source": "self_test",
    }
    ok, reason = is_entry_candidate(strong, args)
    assert ok, reason
    weak = {**strong, "bid": 0.10}
    result = entry_consensus_result(weak, args)
    assert "spread_ok" in result["failed"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stock/price Polymarket paper bot.")
    parser.add_argument("--max-markets", type=int, default=250)
    parser.add_argument("--search-limit", type=int, default=25)
    parser.add_argument("--max-books", type=int, default=80)
    parser.add_argument("--min-edge", type=float, default=0.08)
    parser.add_argument("--min-fair", type=float, default=0.25)
    parser.add_argument("--min-size", type=float, default=5.0)
    parser.add_argument("--max-ask", type=float, default=0.85)
    parser.add_argument("--min-seconds-to-end", type=float, default=300.0)
    parser.add_argument("--min-profit-exit", type=float, default=MIN_ABSOLUTE_PROFIT_EXIT)
    parser.add_argument("--min-roi-exit", type=float, default=MIN_RELATIVE_PROFIT_EXIT)
    parser.add_argument("--fair-collapse", type=float, default=FAIR_COLLAPSE_THRESHOLD)
    parser.add_argument("--stale-bid-over-fair", type=float, default=STALE_BID_OVER_FAIR)
    parser.add_argument("--min-consensus", type=int, default=CONSENSUS_MIN_SIGNALS)
    parser.add_argument("--max-entry-spread", type=float, default=MAX_ENTRY_SPREAD)
    parser.add_argument("--min-edge-to-spread", type=float, default=MIN_EDGE_TO_SPREAD)
    parser.add_argument("--min-reference-distance", type=float, default=MIN_REFERENCE_DISTANCE)
    parser.add_argument("--allow-reentry", action="store_true")
    parser.add_argument("--status", action="store_true", help="Print status even if no entry/exit event happened.")
    parser.add_argument("--self-test", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        run_self_tests()
        print("self-tests passed")
        return 0
    report = run_once(args)
    if report["events"]:
        print("\n".join(report["events"]))
    elif args.status:
        s = report["summary"]
        stats = report["scan_stats"]
        print(
            "Stock paper bot refreshed: "
            f"{report['run_at_utc']} | modeled={stats.get('modeled_markets', 0)} "
            f"rows={report.get('snapshot_rows_written', 0)} actionable={stats.get('actionable_candidates', 0)} "
            f"open={s['open_positions']} closed={s['closed_trades']} pnl={s['paper_pnl_at_100_shares']}"
        )
        print(f"Report: {SUMMARY_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
