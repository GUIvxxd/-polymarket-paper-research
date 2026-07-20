#!/usr/bin/env python3
"""Quiet xtracker Tweet/Post Count market watchdog.

Public-data only. No X API calls, no wallet, no live orders.

Workflow:
1. Refresh xtracker + Gamma + CLOB reports using the existing scanners.
2. Filter for actionable paper candidates with meaningful model edge and depth.
3. Emit stdout only for fresh/materially changed candidates.

Designed for Hermes cron no_agent=True: empty stdout means silent.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/data/workspace/polymarket-research")
REPORTS = ROOT / "reports"
STATE = ROOT / "data" / "xtracker_tweet_watchdog_state.json"
ALERT_LOG = REPORTS / "xtracker_tweet_watchdog_alerts.jsonl"
SNAPSHOT_LOG = REPORTS / "xtracker_tweet_snapshots.jsonl"
SCANNER = ROOT / "xtracker_tweet_edge_scanner.py"
ROBUST = ROOT / "xtracker_tweet_edge_robust.py"
DEPTH = ROOT / "xtracker_tweet_depth_check.py"
DEPTH_REPORT = REPORTS / "xtracker_tweet_depth_latest.json"
ROBUST_REPORT = REPORTS / "xtracker_tweet_edge_robust_latest.json"

# Tightened after rebalance proof showed the strategy was still entering too
# early in fresh weekly markets. The goal is fewer paper entries: prefer
# mid/late-window bucket drift with strong fair value over fresh-window guesses.
FILTER_VERSION = "consensus_v3_2026_07_16"
MIN_EDGE = 0.35
MIN_FAIR = 0.60
MAX_ASK = 0.35
MAX_ENTRY_REMAINING_HOURS = 100.0
EARLY_LOW_BUCKET_REMAINING_HOURS = 48.0
MIN_QTY = 20.0
MIN_COST_LOW = 2.0
MIN_COST_NORMAL = 5.0
CONSENSUS_MIN_SIGNALS = 10
MAX_BOOK_SPREAD = 0.08
MIN_BID_TO_ASK = 0.65
MATERIAL_COUNT_DELTA = 5
MATERIAL_EDGE_DELTA = 0.10
MATERIAL_ASK_DROP = 0.02


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(cmd: list[str]) -> None:
    subprocess.run(
        cmd,
        cwd=str(ROOT),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=420,
    )


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False))


def fnum(x: Any) -> float | None:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


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


def is_low_under_bucket(row: dict[str, Any]) -> bool:
    parsed = parse_bucket(str(row.get("bucket") or ""))
    if not parsed:
        return False
    lo, hi = parsed
    return lo is None and hi is not None and hi <= 19


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
    return float(cap), (row.get("ask_depth_by_cap") or {}).get(cap)


def book_prices(row: dict[str, Any]) -> tuple[float | None, float | None]:
    ask_level = row.get("best_ask_book") or []
    bid_level = row.get("best_bid_book") or []
    best_ask = fnum(ask_level[0]) if isinstance(ask_level, list) and ask_level else None
    best_bid = fnum(bid_level[0]) if isinstance(bid_level, list) and bid_level else None
    return best_ask, best_bid


def consensus_result(row: dict[str, Any]) -> dict[str, Any]:
    """Score independent entry checks before a paper X entry can fire.

    This is the practical version of the "many simulations agree" idea: no
    single model edge is enough. A candidate must also have acceptable price,
    time window, confidence, depth, and live order-book support.
    """
    edge = fnum(row.get("edge"))
    fair = fnum(row.get("fair"))
    ask = fnum(row.get("ask"))
    remaining = fnum(row.get("remaining_hours"))
    cap, depth = depth_for_candidate(row)
    qty = fnum((depth or {}).get("qty")) or 0.0
    cost = fnum((depth or {}).get("cost")) or 0.0
    min_cost = MIN_COST_LOW if ask is not None and ask <= 0.10 else MIN_COST_NORMAL
    best_ask, best_bid = book_prices(row)
    spread = best_ask - best_bid if best_ask is not None and best_bid is not None else None
    bid_to_ask = best_bid / best_ask if best_ask and best_bid is not None else None

    signals: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: str) -> None:
        signals.append({"name": name, "pass": bool(passed), "detail": detail})

    add("model_inputs_present", edge is not None and fair is not None and ask is not None, f"edge={edge};fair={fair};ask={ask}")
    add("medium_high_confidence", row.get("confidence") == "medium_high", f"confidence={row.get('confidence')}")
    add("edge_above_min", edge is not None and edge >= MIN_EDGE, f"edge={edge};min={MIN_EDGE}")
    add("fair_above_min", fair is not None and fair >= MIN_FAIR, f"fair={fair};min={MIN_FAIR}")
    add("ask_below_max", ask is not None and ask <= MAX_ASK, f"ask={ask};max={MAX_ASK}")
    add("time_window_ok", remaining is None or remaining <= MAX_ENTRY_REMAINING_HOURS, f"remaining_h={remaining};max={MAX_ENTRY_REMAINING_HOURS}")
    add("low_bucket_not_too_early", not (is_low_under_bucket(row) and remaining is not None and remaining > EARLY_LOW_BUCKET_REMAINING_HOURS), f"remaining_h={remaining};low_under={is_low_under_bucket(row)}")
    add("live_book_ask_present", best_ask is not None, f"best_ask={best_ask}")
    add("book_spread_ok", spread is not None and spread <= MAX_BOOK_SPREAD, f"spread={spread};max={MAX_BOOK_SPREAD}")
    add("bid_support_ok", bid_to_ask is not None and bid_to_ask >= MIN_BID_TO_ASK, f"bid_to_ask={bid_to_ask};min={MIN_BID_TO_ASK}")
    add("depth_qty_ok", qty >= MIN_QTY, f"qty={qty};min={MIN_QTY};cap={cap}")
    add("depth_cost_ok", cost >= min_cost, f"cost={cost};min={min_cost};cap={cap}")

    passed = sum(1 for signal in signals if signal["pass"])
    failed = [signal["name"] for signal in signals if not signal["pass"]]
    return {
        "version": FILTER_VERSION,
        "passed": passed,
        "required": CONSENSUS_MIN_SIGNALS,
        "total": len(signals),
        "failed": failed,
        "signals": signals,
    }


def consensus_note(result: dict[str, Any]) -> str:
    failed = result.get("failed") or []
    failed_text = ",".join(failed[:5]) if failed else "none"
    return f"consensus={result.get('passed')}/{result.get('total')} required={result.get('required')} failed={failed_text}"


def is_actionable(row: dict[str, Any]) -> tuple[bool, str]:
    edge = fnum(row.get("edge"))
    fair = fnum(row.get("fair"))
    ask = fnum(row.get("ask"))
    remaining = fnum(row.get("remaining_hours"))
    consensus = consensus_result(row)
    note = consensus_note(consensus)
    if edge is None or fair is None or ask is None:
        return False, f"missing_price_or_model;{note}"
    if row.get("confidence") == "low":
        return False, f"low_confidence;{note}"
    if remaining is not None and remaining > MAX_ENTRY_REMAINING_HOURS:
        return False, f"remaining_hours_above_{MAX_ENTRY_REMAINING_HOURS};{note}"
    if row.get("confidence") != "medium_high" and remaining is not None and remaining > 72.0:
        return False, f"needs_medium_high_or_late_window;{note}"
    if is_low_under_bucket(row) and remaining is not None and remaining > EARLY_LOW_BUCKET_REMAINING_HOURS:
        return False, f"low_under_bucket_too_early;{note}"
    if ask > MAX_ASK:
        return False, f"ask_above_max;{note}"
    if edge < MIN_EDGE:
        return False, f"edge_below_threshold;{note}"
    if fair < MIN_FAIR:
        return False, f"fair_below_threshold;{note}"
    if not row.get("best_ask_book"):
        return False, f"missing_book_ask;{note}"

    cap, depth = depth_for_candidate(row)
    if not depth:
        return False, f"missing_depth;{note}"
    qty = fnum(depth.get("qty")) or 0.0
    cost = fnum(depth.get("cost")) or 0.0
    min_cost = MIN_COST_LOW if ask <= 0.10 else MIN_COST_NORMAL
    if qty < MIN_QTY:
        return False, f"depth_qty_below_{MIN_QTY};{note}"
    if cost < min_cost:
        return False, f"depth_cost_below_{min_cost};{note}"
    if int(consensus.get("passed") or 0) < CONSENSUS_MIN_SIGNALS:
        return False, f"consensus_below_threshold;{note}"
    return True, f"filter={FILTER_VERSION};edge={edge:.3f};fair={fair:.3f};ask={ask:.3f};cap={cap};qty={qty:.2f};cost={cost:.2f};{note}"


def candidate_key(row: dict[str, Any]) -> str:
    return "|".join([
        str(row.get("event", "")),
        str(row.get("handle", "")),
        str(row.get("bucket", "")),
        str(row.get("question", "")),
    ])


def event_key(row: dict[str, Any]) -> str:
    return str(row.get("event") or row.get("question") or candidate_key(row))


def material_change(prev: dict[str, Any] | None, row: dict[str, Any]) -> tuple[bool, list[str]]:
    if not prev:
        return True, ["new_candidate"]
    reasons: list[str] = []
    count = int(row.get("count") or 0)
    pcount = int(prev.get("count") or 0)
    if abs(count - pcount) >= MATERIAL_COUNT_DELTA:
        reasons.append(f"count_changed_{pcount}_to_{count}")
    edge = fnum(row.get("edge")) or 0.0
    pedge = fnum(prev.get("edge")) or 0.0
    if edge - pedge >= MATERIAL_EDGE_DELTA:
        reasons.append(f"edge_up_{pedge:.3f}_to_{edge:.3f}")
    ask = fnum(row.get("ask"))
    pask = fnum(prev.get("ask"))
    if ask is not None and pask is not None and pask - ask >= MATERIAL_ASK_DROP:
        reasons.append(f"ask_down_{pask:.3f}_to_{ask:.3f}")
    status = row.get("status")
    if status and status != prev.get("status"):
        reasons.append(f"status_{prev.get('status')}_to_{status}")
    remaining = fnum(row.get("remaining_hours"))
    prem = fnum(prev.get("remaining_hours"))
    if remaining is not None and remaining <= 6 and (prem is None or prem > 6):
        reasons.append("entered_final_6h")
    return bool(reasons), reasons


def compact_row(row: dict[str, Any]) -> dict[str, Any]:
    cap, depth = depth_for_candidate(row)
    consensus = consensus_result(row)
    return {
        "event": row.get("event"),
        "handle": row.get("handle"),
        "bucket": row.get("bucket"),
        "question": row.get("question"),
        "count": row.get("count"),
        "remaining_hours": row.get("remaining_hours"),
        "projected": row.get("projected"),
        "fair": row.get("fair"),
        "ask": row.get("ask"),
        "edge": row.get("edge"),
        "confidence": row.get("confidence"),
        "status": row.get("status"),
        "cap": cap,
        "depth": depth,
        "consensus": consensus,
        "consensus_passed": consensus.get("passed"),
        "consensus_required": consensus.get("required"),
        "consensus_failed": consensus.get("failed"),
        "best_ask_book": row.get("best_ask_book"),
        "best_bid_book": row.get("best_bid_book"),
        "top_asks": row.get("top_asks"),
        "top_bids": row.get("top_bids"),
        "book_request_started_at": row.get("book_request_started_at"),
        "book_response_received_at": row.get("book_response_received_at"),
        "book_provider_timestamp": row.get("book_provider_timestamp"),
        "book_timing_quality": row.get("book_timing_quality"),
        "rate_meta": row.get("rate_meta"),
        "yes_token_id": row.get("yes_token_id"),
        "condition_id": row.get("condition_id"),
    }


def refresh_reports() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    run([py, str(SCANNER)])
    run([py, str(ROBUST)])
    run([py, str(DEPTH)])


def append_alert_log(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ALERT_LOG.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_historical_event_locks(active_events: set[str], filter_version: str = FILTER_VERSION) -> dict[str, str]:
    """Lock each active event to its first same-version paper-alert bucket.

    This prevents the proof ledger from accumulating new mutually-exclusive
    buckets from one event, but deliberately ignores older filter versions so
    stale pre-consensus alerts do not block the active consensus strategy.
    """
    locks: dict[str, str] = {}
    if not ALERT_LOG.exists():
        return locks
    try:
        for line in ALERT_LOG.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            note = str(row.get("watchdog_note") or "")
            consensus = row.get("consensus") or {}
            same_version = consensus.get("version") == filter_version or f"filter={filter_version}" in note
            if not same_version:
                continue
            ev = event_key(row)
            if ev not in active_events or ev in locks:
                continue
            locks[ev] = str(row.get("key") or candidate_key(row))
    except Exception:
        return locks
    return locks


def append_snapshot(rows: list[dict[str, Any]], generated_at: str) -> None:
    """Append a compact every-run time-series snapshot for later proof analysis.

    Alerts are sparse/deduped. Snapshots are the continuous evidence trail: count,
    ask/bid, depth, projected fair value, and model edge at each 5-minute run.
    """
    SNAPSHOT_LOG.parent.mkdir(parents=True, exist_ok=True)
    compact_rows = []
    for row in rows[:25]:
        c = compact_row(row)
        c["watchdog_actionable"], c["watchdog_filter_note"] = is_actionable(row)
        compact_rows.append(c)
    record = {
        "generated_at": generated_at,
        "row_count": len(rows),
        "rows": compact_rows,
    }
    with SNAPSHOT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    refresh_reports()
    depth_payload = load_json(DEPTH_REPORT, {"rows": []})
    rows = depth_payload.get("rows", [])
    state = load_json(STATE, {"candidates": {}, "event_locks": {}, "last_run_at": None})
    previous = state.get("candidates", {})
    active_events = {event_key(r) for r in rows if (fnum(r.get("remaining_hours")) or 0.0) > 0}
    state_filter_version = state.get("filter_version")
    state_locks_version = state.get("event_locks_filter_version")
    event_locks: dict[str, str] = load_historical_event_locks(active_events, FILTER_VERSION)
    if state_filter_version == FILTER_VERSION and state_locks_version == FILTER_VERSION:
        for ev, key in dict(state.get("event_locks") or {}).items():
            if ev in active_events and ev not in event_locks:
                event_locks[ev] = str(key)
    current: dict[str, dict[str, Any]] = {}
    fresh_alerts: list[dict[str, Any]] = []

    # First filter and sort by edge, then keep only the strongest bucket per
    # event. This avoids paper-buying multiple mutually exclusive buckets in
    # the same market, which caused noisy first-batch proof results.
    eligible: list[tuple[float, dict[str, Any], str]] = []
    for row in rows:
        ok, note = is_actionable(row)
        if not ok:
            continue
        eligible.append((fnum(row.get("edge")) or 0.0, row, note))
    selected_events: set[str] = set()
    selected = []
    for _edge, row, note in sorted(eligible, key=lambda x: x[0], reverse=True):
        ev = event_key(row)
        if ev in selected_events:
            continue
        selected_events.add(ev)
        selected.append((row, note))

    event_locks = {ev: key for ev, key in event_locks.items() if ev in active_events}

    for row, note in selected:
        ev = event_key(row)
        key = candidate_key(row)
        locked_key = event_locks.get(ev)
        if locked_key and locked_key != key:
            # Keep observing in snapshots, but do not open a second paper entry
            # for a different bucket in the same dated market event.
            continue
        event_locks.setdefault(ev, key)
        compact = compact_row(row)
        compact["watchdog_note"] = note
        compact["last_seen_at"] = utcnow()
        changed, reasons = material_change(previous.get(key), compact)
        current[key] = compact
        if changed:
            fresh_alerts.append({"key": key, "reasons": reasons, **compact})

    # Preserve previously seen inactive candidates briefly for comparison, but keep state small.
    state_out = {
        "last_run_at": utcnow(),
        "reports": {
            "robust": str(ROBUST_REPORT),
            "depth": str(DEPTH_REPORT),
            "snapshots": str(SNAPSHOT_LOG),
        },
        "filter_version": FILTER_VERSION,
        "entry_rules": {
            "min_edge": MIN_EDGE,
            "min_fair": MIN_FAIR,
            "max_ask": MAX_ASK,
            "max_entry_remaining_hours": MAX_ENTRY_REMAINING_HOURS,
            "early_low_bucket_remaining_hours": EARLY_LOW_BUCKET_REMAINING_HOURS,
            "min_qty": MIN_QTY,
            "min_cost_low": MIN_COST_LOW,
            "min_cost_normal": MIN_COST_NORMAL,
            "consensus_min_signals": CONSENSUS_MIN_SIGNALS,
            "max_book_spread": MAX_BOOK_SPREAD,
            "min_bid_to_ask": MIN_BID_TO_ASK,
        },
        "candidates": current,
        "event_locks_filter_version": FILTER_VERSION,
        "event_locks": event_locks,
    }
    save_json(STATE, state_out)
    append_snapshot(rows, state_out["last_run_at"])
    append_alert_log(fresh_alerts)

    if not fresh_alerts:
        return 0

    print("Polymarket xtracker Tweet/Post watchdog found fresh paper candidates")
    print(f"generated_at={state_out['last_run_at']}")
    print(f"depth_report={DEPTH_REPORT}")
    print(f"state_file={STATE}")
    print("paper_only=true no_x_api_spend=true no_live_orders=true")
    for item in sorted(fresh_alerts, key=lambda x: fnum(x.get("edge")) or 0.0, reverse=True)[:8]:
        depth = item.get("depth") or {}
        print()
        print(f"{item.get('event')} | bucket={item.get('bucket')} | confidence={item.get('confidence')}")
        print(f"reasons={','.join(item.get('reasons') or [])}")
        print(f"count={item.get('count')} remaining_h={item.get('remaining_hours')} projected={item.get('projected')}")
        print(f"ask={item.get('ask')} fair={item.get('fair')} edge={item.get('edge')} cap={item.get('cap')} depth_qty={depth.get('qty')} depth_cost={depth.get('cost')} depth_avg={depth.get('avg')}")
        print(f"consensus={item.get('consensus_passed')}/{item.get('consensus', {}).get('total')} required={item.get('consensus_required')} failed={','.join(item.get('consensus_failed') or []) or 'none'}")
        print(f"question={item.get('question')}")
    print()
    print("Validate manually before any real-money action; this is a paper-trading alert.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
