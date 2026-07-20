#!/usr/bin/env python3
"""Paper-only rebalance/exit scanner for xtracker Tweet/Post markets.

Reads the existing xtracker reports and paper-proof entries, then compares old
paper YES entries against the current model + CLOB book. It is designed to catch
bucket drift: the original bucket may no longer be the best expected outcome,
but the market may still bid it high enough to exit or even profit.

Public-data only: no X API, no wallet, no live orders.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import statistics
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path("/data/workspace/polymarket-research")
REPORTS = ROOT / "reports"
RAW = REPORTS / "xtracker_tweet_edge_latest.json"
PROOF = REPORTS / "xtracker_paper_proof_latest.json"
OUT_JSON = REPORTS / "xtracker_rebalance_opportunities_latest.json"
OUT_MD = REPORTS / "xtracker_rebalance_opportunities_latest.md"
UA = "Hermes-XTracker-Rebalance/0.1"

MIN_SWITCH_EDGE = 0.25
MIN_SWITCH_FAIR = 0.45
MIN_EXIT_PROFIT = 0.02
MIN_STALE_BID_EDGE = 0.10


def parse_dt(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def fnum(x: Any) -> float | None:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def parse_jsonish(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, list) else []
        except Exception:
            return []
    return []


def parse_bucket(label: str) -> tuple[int | None, int | None] | None:
    s = (label or "").replace("\\u003c", "<").strip()
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
            a, b = s.split("-", 1)
            return int(a), int(b)
        except Exception:
            return None
    return None


def poisson_cdf(k: int, lam: float) -> float:
    if k < 0:
        return 0.0
    term = math.exp(-lam)
    total = term
    for i in range(1, k + 1):
        term *= lam / i
        total += term
    return min(max(total, 0.0), 1.0)


def pr_range(lo: int | None, hi: int | None, lam: float) -> float:
    if lo is None and hi is not None:
        return poisson_cdf(hi, lam)
    if lo is not None and hi is None:
        return 1 - poisson_cdf(lo - 1, lam)
    if lo is not None and hi is not None:
        return poisson_cdf(hi, lam) - poisson_cdf(lo - 1, lam)
    return 0.0


def hourly_counts(stats: dict[str, Any]) -> list[tuple[dt.datetime, int]]:
    out: list[tuple[dt.datetime, int]] = []
    for row in stats.get("daily") or []:
        try:
            out.append((parse_dt(row["date"]), int(row.get("count") or 0)))
        except Exception:
            continue
    return sorted(out)


def recent_rate(stats: dict[str, Any], hours: int = 24) -> tuple[float | None, int]:
    rows = hourly_counts(stats)
    if not rows:
        return None, 0
    latest = max(t for t, _ in rows)
    cutoff = latest - dt.timedelta(hours=hours - 1e-9)
    vals = [c for t, c in rows if t >= cutoff]
    if not vals:
        return None, 0
    return sum(vals) / min(hours, max(1, len(vals))), sum(vals)


def event_rate(event: dict[str, Any]) -> float:
    elapsed = max(float(event.get("elapsed_hours") or 0), 0.01)
    return int(event.get("count_now") or 0) / elapsed


def choose_rate(event: dict[str, Any], baseline24: float | None) -> tuple[float, dict[str, Any]]:
    elapsed = float(event.get("elapsed_hours") or 0)
    r_event = event_rate(event)
    r24, c24 = recent_rate(event.get("stats") or {}, 24)
    notes: list[str] = []
    if elapsed < 8:
        rate = baseline24 if baseline24 is not None else (r24 if r24 is not None else r_event)
        notes.append("fresh_window_use_handle_baseline")
    elif r24 is not None:
        if r24 > r_event:
            rate = 0.75 * r_event + 0.25 * r24
            notes.append("conservative_event_weighted_rate_spike_capped")
        else:
            rate = 0.60 * r_event + 0.40 * r24
            notes.append("conservative_event_weighted_rate_slowdown_allowed")
    else:
        rate = r_event
        notes.append("event_rate_only")
    if baseline24 is not None and elapsed >= 8 and baseline24 < rate:
        rate = 0.70 * rate + 0.30 * baseline24
        notes.append("downward_shrunk_to_handle_baseline")
    confidence = "low" if elapsed < 8 else ("medium" if elapsed < 36 else "medium_high")
    return rate, {
        "event_rate_h": round(r_event, 3),
        "last24_rate_h": None if r24 is None else round(r24, 3),
        "last24_count": c24,
        "baseline24_rate_h": None if baseline24 is None else round(baseline24, 3),
        "notes": notes,
        "confidence": confidence,
    }


def market_price(market: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    best_ask = fnum(market.get("bestAsk"))
    best_bid = fnum(market.get("bestBid"))
    prices = parse_jsonish(market.get("outcomePrices"))
    yes_mid = fnum(prices[0]) if prices else None
    return (best_ask if best_ask is not None else yes_mid), best_ask, best_bid


def fetch_book(token_id: str | None) -> dict[str, Any] | None:
    if not token_id:
        return None
    url = "https://clob.polymarket.com/book?" + urllib.parse.urlencode({"token_id": token_id})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode())
    except Exception:
        return None


def top_book(book: dict[str, Any] | None) -> tuple[list[float] | None, list[float] | None]:
    if not book:
        return None, None
    asks = sorted(
        [(float(x["price"]), float(x["size"])) for x in book.get("asks", [])],
        key=lambda x: x[0],
    )
    bids = sorted(
        [(float(x["price"]), float(x["size"])) for x in book.get("bids", [])],
        key=lambda x: x[0],
        reverse=True,
    )
    return (list(asks[0]) if asks else None), (list(bids[0]) if bids else None)


def build_current_models(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    events = [event for event in raw.get("events", []) if isinstance(event, dict) and not event.get("error")]
    by_handle: dict[str, list[float]] = {}
    for event in events:
        if float(event.get("elapsed_hours") or 0) >= 24:
            r24, _ = recent_rate(event.get("stats") or {}, 24)
            if r24 is not None:
                by_handle.setdefault(event["handle"], []).append(r24)
    baseline = {handle: statistics.median(vals) for handle, vals in by_handle.items() if vals}

    out: dict[str, dict[str, Any]] = {}
    for event in events:
        title = event.get("title") or ""
        count = int(event.get("count_now") or 0)
        remaining = float(event.get("remaining_hours") or 0)
        rate, meta = choose_rate(event, baseline.get(event.get("handle")))
        lam = max(rate * remaining, 0)
        buckets = []
        for market in event.get("buckets") or []:
            parsed = parse_bucket(market.get("bucket") or "")
            if not parsed:
                continue
            lo, hi = parsed
            if hi is None and lo is not None and count >= lo:
                fair = 1.0
                status = "locked_yes"
            elif hi is not None and count > hi:
                fair = 0.0
                status = "already_over_bucket"
            else:
                add_lo = None if lo is None else max(lo - count, 0)
                add_hi = None if hi is None else hi - count
                fair = 0.0 if add_hi is not None and add_hi < 0 else pr_range(add_lo, add_hi, lam)
                status = "model"
            buy, ask, bid = market_price(market)
            tokens = parse_jsonish(market.get("clobTokenIds"))
            edge = None if buy is None else fair - float(buy)
            buckets.append({
                "bucket": market.get("bucket"),
                "question": market.get("question"),
                "market_id": market.get("market_id") or market.get("id"),
                "count": count,
                "remaining_hours": round(remaining, 2),
                "projected": round(count + lam, 2),
                "fair": round(fair, 4),
                "ask": ask,
                "bid": bid,
                "edge": None if edge is None else round(edge, 4),
                "status": status,
                "yes_token_id": tokens[0] if tokens else None,
                "condition_id": market.get("condition_id") or market.get("conditionId"),
                "confidence": meta["confidence"],
                "rate_meta": meta,
            })
        best_switch = None
        eligible = [
            b for b in buckets
            if b.get("confidence") != "low"
            and fnum(b.get("edge")) is not None
            and fnum(b.get("fair")) is not None
            and fnum(b.get("ask")) is not None
            and float(b["edge"]) >= MIN_SWITCH_EDGE
            and float(b["fair"]) >= MIN_SWITCH_FAIR
        ]
        if eligible:
            best_switch = sorted(eligible, key=lambda b: float(b["edge"]), reverse=True)[0]
        out[title] = {
            "event": title,
            "handle": event.get("handle"),
            "platform": event.get("platform"),
            "count": count,
            "remaining_hours": round(remaining, 2),
            "rate_meta": meta,
            "buckets": buckets,
            "best_switch": best_switch,
        }
    return out


def main() -> int:
    raw = json.loads(RAW.read_text())
    proof = json.loads(PROOF.read_text())
    current = build_current_models(raw)
    # The proof tracker materializes pending entries under `next_pending` and
    # resolved entries under `resolved_entries`; there is no flat `entries` key.
    pending = [entry for entry in proof.get("next_pending", []) if entry.get("status") == "pending"]
    rows = []
    for entry in pending:
        event = entry.get("event") or ""
        model_event = current.get(event)
        if not model_event:
            continue
        held_bucket = entry.get("bucket")
        held_current = next((b for b in model_event["buckets"] if b.get("bucket") == held_bucket), None)
        if not held_current:
            continue
        book = fetch_book(entry.get("yes_token_id") or held_current.get("yes_token_id"))
        best_ask_book, best_bid_book = top_book(book)
        entry_price = fnum(entry.get("entry_price"))
        current_bid = best_bid_book[0] if best_bid_book else fnum(held_current.get("bid"))
        current_ask = best_ask_book[0] if best_ask_book else fnum(held_current.get("ask"))
        held_fair = fnum(held_current.get("fair"))
        best_switch = model_event.get("best_switch")
        switch_bucket = best_switch.get("bucket") if best_switch else None
        exit_profit = None if current_bid is None or entry_price is None else current_bid - entry_price
        exit_roi = None if exit_profit is None or not entry_price else exit_profit / entry_price
        stale_bid_edge = None if current_bid is None or held_fair is None else current_bid - held_fair
        reasons = []
        if exit_profit is not None and exit_profit >= MIN_EXIT_PROFIT:
            reasons.append("can_exit_for_profit")
        if stale_bid_edge is not None and stale_bid_edge >= MIN_STALE_BID_EDGE:
            reasons.append("market_bid_above_current_model")
        if best_switch and switch_bucket != held_bucket:
            reasons.append("better_bucket_available")
        if not reasons:
            continue
        rows.append({
            "event": event,
            "handle": entry.get("handle"),
            "held_bucket": held_bucket,
            "held_entry_price": entry_price,
            "held_entry_time": entry.get("entry_time"),
            "held_entry_count": entry.get("entry_count"),
            "current_count": model_event.get("count"),
            "remaining_hours": model_event.get("remaining_hours"),
            "held_current_fair": held_fair,
            "held_current_bid": current_bid,
            "held_current_ask": current_ask,
            "exit_profit_per_share": None if exit_profit is None else round(exit_profit, 4),
            "exit_roi_on_entry": None if exit_roi is None else round(exit_roi, 4),
            "stale_bid_edge": None if stale_bid_edge is None else round(stale_bid_edge, 4),
            "best_switch_bucket": None if not best_switch else best_switch.get("bucket"),
            "best_switch_ask": None if not best_switch else best_switch.get("ask"),
            "best_switch_fair": None if not best_switch else best_switch.get("fair"),
            "best_switch_edge": None if not best_switch else best_switch.get("edge"),
            "best_switch_projected": None if not best_switch else best_switch.get("projected"),
            "best_switch_question": None if not best_switch else best_switch.get("question"),
            "rate_meta": model_event.get("rate_meta"),
            "reasons": reasons,
        })
    rows = sorted(rows, key=lambda r: (r.get("stale_bid_edge") or 0, r.get("best_switch_edge") or 0), reverse=True)
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": {
            "raw": str(RAW),
            "proof": str(PROOF),
        },
        "paper_only": True,
        "rules": {
            "min_switch_edge": MIN_SWITCH_EDGE,
            "min_switch_fair": MIN_SWITCH_FAIR,
            "min_exit_profit": MIN_EXIT_PROFIT,
            "min_stale_bid_edge": MIN_STALE_BID_EDGE,
        },
        "rows": rows,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    OUT_MD.write_text(make_markdown(payload))
    print(json.dumps({"report": str(OUT_JSON), "markdown": str(OUT_MD), "rows": rows[:12]}, indent=2, ensure_ascii=False))
    return 0


def make_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# xtracker Rebalance Opportunities",
        "",
        f"Generated: `{payload['generated_at']}`",
        "",
        "Paper-only research. No X API spend, no wallet, no live orders.",
        "",
        "## Interpretation",
        "",
        "This scanner looks for old paper YES entries whose bucket has drifted away from the current model, while the CLOB still has a bid high enough to exit or profit. It also shows the current best replacement bucket for the same dated event.",
        "",
        "| Event | Held bucket | Entry | Current fair | Current bid | Exit ROI | Best switch | Switch ask | Switch fair | Switch edge | Reasons |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in payload.get("rows", [])[:25]:
        lines.append(
            "| {event} | `{held_bucket}` | {held_entry_price} | {held_current_fair} | {held_current_bid} | {exit_roi_on_entry} | `{best_switch_bucket}` | {best_switch_ask} | {best_switch_fair} | {best_switch_edge} | {reasons} |".format(
                event=row.get("event"),
                held_bucket=row.get("held_bucket"),
                held_entry_price=row.get("held_entry_price"),
                held_current_fair=row.get("held_current_fair"),
                held_current_bid=row.get("held_current_bid"),
                exit_roi_on_entry=row.get("exit_roi_on_entry"),
                best_switch_bucket=row.get("best_switch_bucket"),
                best_switch_ask=row.get("best_switch_ask"),
                best_switch_fair=row.get("best_switch_fair"),
                best_switch_edge=row.get("best_switch_edge"),
                reasons=", ".join(row.get("reasons") or []),
            )
        )
    if not payload.get("rows"):
        lines.append("| _No rebalance opportunities found_ | | | | | | | | | | |")
    lines += [
        "",
        "## Suggested strategy change",
        "",
        "Do not lock a dated event to the first alerted bucket until settlement. Track mark-to-market exits and bucket switches. A paper position should be allowed to exit when either: (1) bid is above entry by a defined profit threshold, or (2) bid is materially above current model fair and another bucket in the same event has stronger fair/edge.",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
