#!/usr/bin/env python3
"""Polymarket public-data strategy scanner for paper research.

Scope:
- No authentication
- No wallet/private keys
- No live orders
- Public Gamma + public CLOB reads only
- Reports paper-trading candidates, not trade instructions
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
UA = "Hermes-Polymarket-Paper-Strategy-Scanner/0.2"


@dataclass
class BookTop:
    token_id: str
    outcome: str
    bid: float | None
    bid_size: float | None
    ask: float | None
    ask_size: float | None
    spread: float | None
    mid: float | None
    available: bool
    error: str | None = None


@dataclass
class Candidate:
    strategy: str
    severity: str
    slug: str
    question: str
    condition_id: str
    end_date: str | None
    seconds_to_end: float | None
    outcomes: str
    executable_score: int
    values: str
    note: str


def get_json(url: str, params: dict[str, Any] | None = None, timeout: float = 20.0) -> Any:
    if params:
        url = url + "?" + urlencode(params, doseq=True)
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def parse_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        try:
            out = json.loads(value)
        except json.JSONDecodeError:
            return []
        return out if isinstance(out, list) else []
    return []


def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def market_str(m: dict[str, Any], *keys: str) -> str:
    for k in keys:
        value = m.get(k)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def parse_end(end_date: str | None) -> tuple[datetime | None, float | None]:
    if not end_date:
        return None, None
    try:
        dt = datetime.fromisoformat(end_date.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None, None
    return dt, (dt - datetime.now(UTC)).total_seconds()


def top_book(token_id: str, outcome: str) -> BookTop:
    try:
        payload = get_json(f"{CLOB}/book", {"token_id": token_id}, timeout=12.0)
    except HTTPError as e:
        return BookTop(token_id, outcome, None, None, None, None, None, None, False, f"HTTP {e.code}")
    except (URLError, TimeoutError, OSError) as e:
        return BookTop(token_id, outcome, None, None, None, None, None, None, False, type(e).__name__)

    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    for item in payload.get("bids") or []:
        p, s = to_float(item.get("price")), to_float(item.get("size"))
        if p is not None and s is not None:
            bids.append((p, s))
    for item in payload.get("asks") or []:
        p, s = to_float(item.get("price")), to_float(item.get("size"))
        if p is not None and s is not None:
            asks.append((p, s))

    bid = max((p for p, _ in bids), default=None)
    ask = min((p for p, _ in asks), default=None)
    bid_size = next((s for p, s in bids if p == bid), None) if bid is not None else None
    ask_size = next((s for p, s in asks if p == ask), None) if ask is not None else None
    spread = ask - bid if ask is not None and bid is not None else None
    mid = (ask + bid) / 2 if ask is not None and bid is not None else None
    return BookTop(token_id, outcome, bid, bid_size, ask, ask_size, spread, mid, True)


def fetch_markets(max_markets: int, page_size: int = 100) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    offset = 0
    while len(out) < max_markets:
        limit = min(page_size, max_markets - len(out))
        page = get_json(
            f"{GAMMA}/markets",
            {"active": "true", "closed": "false", "limit": limit, "offset": offset},
        )
        if not isinstance(page, list) or not page:
            break
        out.extend([m for m in page if isinstance(m, dict)])
        if len(page) < limit:
            break
        offset += limit
    return out[:max_markets]


def candidate(
    strategy: str,
    severity: str,
    m: dict[str, Any],
    outcomes: list[str],
    seconds_to_end: float | None,
    executable_score: int,
    values: str,
    note: str,
) -> Candidate:
    return Candidate(
        strategy=strategy,
        severity=severity,
        slug=market_str(m, "slug"),
        question=market_str(m, "question", "title"),
        condition_id=market_str(m, "conditionId"),
        end_date=market_str(m, "endDate") or None,
        seconds_to_end=seconds_to_end,
        outcomes="/".join(outcomes),
        executable_score=executable_score,
        values=values,
        note=note,
    )


def scan(markets: list[dict[str, Any]], max_book_markets: int, sleep_s: float) -> tuple[list[Candidate], dict[str, Any]]:
    candidates: list[Candidate] = []
    stats = {
        "retrieved_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "markets_scanned": len(markets),
        "binary_markets": 0,
        "multi_outcome_markets": 0,
        "markets_with_clob_ids": 0,
        "book_markets_scanned": 0,
        "book_requests": 0,
        "book_errors": 0,
        "candidates": 0,
    }

    now = datetime.now(UTC)
    for m in markets:
        outcomes = [str(x) for x in parse_list(m.get("outcomes"))]
        prices = [to_float(x) for x in parse_list(m.get("outcomePrices"))]
        token_ids = [str(x) for x in parse_list(m.get("clobTokenIds"))]
        end_dt, seconds_to_end = parse_end(market_str(m, "endDate") or None)

        if len(outcomes) == 2:
            stats["binary_markets"] += 1
        elif len(outcomes) > 2:
            stats["multi_outcome_markets"] += 1
        if token_ids:
            stats["markets_with_clob_ids"] += 1

        if end_dt and end_dt < now:
            candidates.append(candidate(
                "stale_expired_active",
                "medium",
                m,
                outcomes,
                seconds_to_end,
                1,
                "",
                "Gamma says active/open but endDate is in the past. This is a watchlist/resolution-research signal, not an automatic executable edge.",
            ))

        if prices and all(p is not None for p in prices) and len(prices) == len(outcomes):
            price_sum = sum(p for p in prices if p is not None)
            if len(outcomes) >= 2 and abs(price_sum - 1.0) >= 0.05:
                candidates.append(candidate(
                    "gamma_price_sum_mismatch",
                    "low",
                    m,
                    outcomes,
                    seconds_to_end,
                    0,
                    f"gamma_sum={price_sum:.4f}; prices={','.join(f'{p:.4f}' for p in prices if p is not None)}",
                    "Gamma metadata prices do not sum near 1. Usually stale metadata; use only as a lead for CLOB validation.",
                ))

        if len(token_ids) < 2 or len(token_ids) != len(outcomes):
            continue
        if stats["book_markets_scanned"] >= max_book_markets:
            continue

        books: list[BookTop] = []
        for token_id, outcome in zip(token_ids, outcomes, strict=False):
            b = top_book(token_id, outcome)
            stats["book_requests"] += 1
            if not b.available:
                stats["book_errors"] += 1
            books.append(b)
            time.sleep(sleep_s)
        stats["book_markets_scanned"] += 1
        valid_books = [b for b in books if b.available]
        if len(valid_books) != len(books):
            continue

        ask_books = [b for b in books if b.ask is not None and b.ask_size is not None and b.ask_size > 0]
        bid_books = [b for b in books if b.bid is not None and b.bid_size is not None and b.bid_size > 0]

        # Buy-all package arbitrage: complete exhaustive outcomes at asks sum < 1.
        if len(ask_books) == len(outcomes):
            ask_sum = sum(b.ask for b in ask_books if b.ask is not None)
            min_size = min(b.ask_size for b in ask_books if b.ask_size is not None)
            profit = 1 - ask_sum
            if ask_sum < 0.995 and min_size >= 5:
                candidates.append(candidate(
                    "buy_all_outcomes_arbitrage",
                    "high" if ask_sum < 0.98 else "medium",
                    m,
                    outcomes,
                    seconds_to_end,
                    3,
                    f"ask_sum={ask_sum:.4f}; gross_profit_per_set={profit:.4f}; min_top_size={min_size:.2f}",
                    "Paper edge: buying every mutually exclusive outcome below $1. Needs depth/latency/fill validation and market completeness review.",
                ))

        # Sell-all/inventory or mint/redeem-style overpricing signal.
        if len(bid_books) == len(outcomes):
            bid_sum = sum(b.bid for b in bid_books if b.bid is not None)
            min_size = min(b.bid_size for b in bid_books if b.bid_size is not None)
            excess = bid_sum - 1
            if bid_sum > 1.005 and min_size >= 5:
                candidates.append(candidate(
                    "sell_all_or_mint_arbitrage",
                    "medium",
                    m,
                    outcomes,
                    seconds_to_end,
                    2,
                    f"bid_sum={bid_sum:.4f}; excess={excess:.4f}; min_top_size={min_size:.2f}",
                    "Overpricing signal. Real execution generally requires inventory or mint/redeem mechanics; paper only.",
                ))

        for b in books:
            if b.bid is not None and b.ask is not None and b.bid > b.ask:
                candidates.append(candidate(
                    "crossed_book",
                    "high",
                    m,
                    [b.outcome],
                    seconds_to_end,
                    3,
                    f"bid={b.bid:.4f}; ask={b.ask:.4f}; crossed={b.bid-b.ask:.4f}",
                    "Crossed public book. Often transient/stale; needs immediate repeated polling before paper or live assumptions.",
                ))
            if b.spread is not None and b.bid is not None and b.ask is not None and b.mid is not None:
                if b.spread >= 0.08 and 0.10 <= b.mid <= 0.90 and (b.bid_size or 0) >= 10 and (b.ask_size or 0) >= 10:
                    candidates.append(candidate(
                        "wide_spread_market_making",
                        "low",
                        m,
                        [b.outcome],
                        seconds_to_end,
                        1,
                        f"bid={b.bid:.4f}; ask={b.ask:.4f}; spread={b.spread:.4f}; mid={b.mid:.4f}; top_sizes={b.bid_size:.2f}/{b.ask_size:.2f}",
                        "Market-making candidate. Expected value depends on queue priority, fills, adverse selection, and fees/slippage.",
                    ))
                if seconds_to_end is not None and 0 <= seconds_to_end <= 3600 and (b.ask <= 0.03 or b.bid >= 0.97):
                    candidates.append(candidate(
                        "near_expiry_tail_or_certainty",
                        "low",
                        m,
                        [b.outcome],
                        seconds_to_end,
                        1,
                        f"bid={b.bid:.4f}; ask={b.ask:.4f}; seconds_to_end={seconds_to_end:.0f}",
                        "Near-expiry extreme price. Needs independent truth model; not an edge from price alone.",
                    ))

        if prices and len(prices) == len(books):
            for p, b in zip(prices, books, strict=False):
                if p is not None and b.mid is not None and abs(p - b.mid) >= 0.08:
                    candidates.append(candidate(
                        "gamma_vs_clob_staleness",
                        "low",
                        m,
                        [b.outcome],
                        seconds_to_end,
                        0,
                        f"gamma={p:.4f}; clob_mid={b.mid:.4f}; diff={p-b.mid:.4f}",
                        "Metadata-vs-book mismatch. Mostly useful to avoid stale signals, not a standalone edge.",
                    ))

    stats["candidates"] = len(candidates)
    return candidates, stats


def write_outputs(candidates: list[Candidate], stats: dict[str, Any], outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = stats["retrieved_at_utc"].replace(":", "").replace("-", "").replace(".", "_")
    payload = {"stats": stats, "candidates": [asdict(c) for c in candidates]}
    latest = outdir / "strategy_scan_latest.json"
    json_path = outdir / f"strategy_scan_{stamp}.json"
    csv_path = outdir / f"strategy_scan_{stamp}.csv"
    latest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    fields = list(Candidate.__annotations__.keys())
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for c in candidates:
            writer.writerow(asdict(c))
    print(f"latest={latest}")
    print(f"json={json_path}")
    print(f"csv={csv_path}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-markets", type=int, default=300)
    ap.add_argument("--max-book-markets", type=int, default=160)
    ap.add_argument("--sleep", type=float, default=0.03)
    ap.add_argument("--outdir", type=Path, default=Path("/data/workspace/polymarket-research/reports"))
    args = ap.parse_args()

    markets = fetch_markets(args.max_markets)
    candidates, stats = scan(markets, args.max_book_markets, args.sleep)
    rank = {"high": 0, "medium": 1, "low": 2}
    candidates.sort(key=lambda c: (rank.get(c.severity, 9), -c.executable_score, c.strategy, c.slug))

    print(json.dumps(stats, indent=2))
    print("\nTop candidates:")
    for c in candidates[:30]:
        print(f"[{c.severity}/score={c.executable_score}] {c.strategy} | {c.slug} | {c.outcomes} | {c.values}")
    write_outputs(candidates, stats, args.outdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
