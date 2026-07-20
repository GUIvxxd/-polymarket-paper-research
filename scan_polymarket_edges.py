#!/usr/bin/env python3
"""Public Polymarket paper-research scanner.

No auth, no wallet, no live trading. Fetches Gamma market metadata and public CLOB books,
then reports candidate paper-trading edges.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import time
from dataclasses import dataclass, asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
UA = "Hermes-Polymarket-Paper-Scanner/0.1"


@dataclass
class BookTop:
    token_id: str
    bid: float | None
    bid_size: float | None
    ask: float | None
    ask_size: float | None
    spread: float | None
    available: bool
    error: str | None = None


@dataclass
class Candidate:
    kind: str
    severity: str
    slug: str
    question: str
    condition_id: str
    end_date: str | None
    outcomes: str
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
        s = value.strip()
        if not s:
            return []
        try:
            out = json.loads(s)
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


def top_book(token_id: str) -> BookTop:
    try:
        payload = get_json(f"{CLOB}/book", {"token_id": token_id}, timeout=12.0)
    except HTTPError as e:
        return BookTop(token_id, None, None, None, None, None, False, f"HTTP {e.code}")
    except (URLError, TimeoutError, OSError) as e:
        return BookTop(token_id, None, None, None, None, None, False, type(e).__name__)

    bids = []
    asks = []
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
    return BookTop(token_id, bid, bid_size, ask, ask_size, spread, True, None)


def fetch_markets(max_markets: int, page_size: int = 100) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    offset = 0
    while len(markets) < max_markets:
        limit = min(page_size, max_markets - len(markets))
        page = get_json(
            f"{GAMMA}/markets",
            {"active": "true", "closed": "false", "limit": limit, "offset": offset},
        )
        if not isinstance(page, list) or not page:
            break
        markets.extend([m for m in page if isinstance(m, dict)])
        if len(page) < limit:
            break
        offset += limit
    return markets[:max_markets]


def market_str(m: dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = m.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def scan(markets: list[dict[str, Any]], max_books: int, sleep_s: float) -> tuple[list[Candidate], dict[str, Any]]:
    candidates: list[Candidate] = []
    books_seen = 0
    binary_seen = 0
    with_books = 0
    now = datetime.now(UTC)

    for m in markets:
        outcomes = [str(x) for x in parse_list(m.get("outcomes"))]
        prices = [to_float(x) for x in parse_list(m.get("outcomePrices"))]
        token_ids = [str(x) for x in parse_list(m.get("clobTokenIds"))]
        slug = market_str(m, "slug")
        question = market_str(m, "question", "title")
        condition_id = market_str(m, "conditionId")
        end_date = market_str(m, "endDate") or None

        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00")).astimezone(UTC)
                if end_dt < now:
                    candidates.append(Candidate(
                        "expired_active_market",
                        "medium",
                        slug,
                        question,
                        condition_id,
                        end_date,
                        "/".join(outcomes),
                        "",
                        "Gamma says active/open but endDate is in the past; check for stale/resolution edge, not directly tradable without manual review.",
                    ))
            except ValueError:
                pass

        if len(outcomes) == 2:
            binary_seen += 1
            if len(prices) == 2 and prices[0] is not None and prices[1] is not None:
                s = prices[0] + prices[1]
                if abs(s - 1.0) >= 0.03:
                    candidates.append(Candidate(
                        "gamma_price_sum_mismatch",
                        "low",
                        slug,
                        question,
                        condition_id,
                        end_date,
                        "/".join(outcomes),
                        f"gamma_sum={s:.4f} prices={prices[0]:.4f}/{prices[1]:.4f}",
                        "Metadata outcomePrices do not sum near 1; may be stale metadata, not necessarily executable edge.",
                    ))

        if len(outcomes) != 2 or len(token_ids) != 2 or books_seen + 2 > max_books:
            continue

        b0 = top_book(token_ids[0])
        time.sleep(sleep_s)
        b1 = top_book(token_ids[1])
        time.sleep(sleep_s)
        books_seen += 2
        if not (b0.available and b1.available):
            continue
        with_books += 1

        # Pair buy: if best ask on both complementary outcomes sums below 1, buying both is positive payout minus costs.
        if b0.ask is not None and b1.ask is not None:
            ask_sum = b0.ask + b1.ask
            min_ask_size = min(b0.ask_size or 0.0, b1.ask_size or 0.0)
            profit_per_pair = 1.0 - ask_sum
            if ask_sum < 0.995 and min_ask_size >= 5:
                candidates.append(Candidate(
                    "complement_buy_arbitrage",
                    "high" if ask_sum < 0.98 else "medium",
                    slug,
                    question,
                    condition_id,
                    end_date,
                    "/".join(outcomes),
                    f"ask_sum={ask_sum:.4f} profit_per_pair={profit_per_pair:.4f} min_size={min_ask_size:.2f}",
                    "Paper edge: buy both outcomes at visible asks. Needs depth, latency, and fill validation before real action.",
                ))

        # Pair sell / mint-redeem style: bid sum > 1 can indicate overpricing, but may require inventory/minting.
        if b0.bid is not None and b1.bid is not None:
            bid_sum = b0.bid + b1.bid
            min_bid_size = min(b0.bid_size or 0.0, b1.bid_size or 0.0)
            if bid_sum > 1.005 and min_bid_size >= 5:
                candidates.append(Candidate(
                    "complement_sell_or_mint_arbitrage",
                    "medium",
                    slug,
                    question,
                    condition_id,
                    end_date,
                    "/".join(outcomes),
                    f"bid_sum={bid_sum:.4f} excess={bid_sum-1.0:.4f} min_size={min_bid_size:.2f}",
                    "Possible overpricing signal. Real execution usually needs inventory/mint/redeem mechanics; paper only.",
                ))

        for outcome, b in zip(outcomes, (b0, b1), strict=False):
            if b.bid is not None and b.ask is not None and b.bid > b.ask:
                candidates.append(Candidate(
                    "crossed_single_book",
                    "high",
                    slug,
                    question,
                    condition_id,
                    end_date,
                    outcome,
                    f"bid={b.bid:.4f} ask={b.ask:.4f}",
                    "Crossed public book. Likely stale/transient; paper-check immediately.",
                ))
            if b.spread is not None and b.bid is not None and b.ask is not None:
                mid = (b.bid + b.ask) / 2
                if b.spread >= 0.08 and (b.bid_size or 0) >= 10 and (b.ask_size or 0) >= 10 and 0.10 < mid < 0.90:
                    candidates.append(Candidate(
                        "wide_spread_market_making_candidate",
                        "low",
                        slug,
                        question,
                        condition_id,
                        end_date,
                        outcome,
                        f"bid={b.bid:.4f} ask={b.ask:.4f} spread={b.spread:.4f} bid_size={b.bid_size:.2f} ask_size={b.ask_size:.2f}",
                        "Market-making candidate only; edge depends on queue priority, adverse selection, and cancellations.",
                    ))

        if len(prices) == 2 and all(p is not None for p in prices):
            mids = []
            for b in (b0, b1):
                if b.bid is not None and b.ask is not None:
                    mids.append((b.bid + b.ask) / 2)
            if len(mids) == 2:
                for outcome, gamma_p, mid in zip(outcomes, prices, mids, strict=False):
                    if abs(gamma_p - mid) >= 0.08:
                        candidates.append(Candidate(
                            "gamma_vs_clob_mid_mismatch",
                            "low",
                            slug,
                            question,
                            condition_id,
                            end_date,
                            outcome,
                            f"gamma={gamma_p:.4f} clob_mid={mid:.4f} diff={gamma_p-mid:.4f}",
                            "Metadata vs live book mismatch. Usually means Gamma outcomePrices are stale; not an executable edge by itself.",
                        ))

    stats = {
        "retrieved_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "markets_scanned": len(markets),
        "binary_markets": binary_seen,
        "book_requests": books_seen,
        "binary_markets_with_books": with_books,
        "candidates": len(candidates),
    }
    return candidates, stats


def write_outputs(candidates: list[Candidate], stats: dict[str, Any], outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = stats["retrieved_at_utc"].replace(":", "").replace("-", "").replace(".", "_")
    json_path = outdir / f"edge_scan_{stamp}.json"
    csv_path = outdir / f"edge_scan_{stamp}.csv"
    latest_path = outdir / "edge_scan_latest.json"
    payload = {"stats": stats, "candidates": [asdict(c) for c in candidates]}
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    latest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(candidates[0]).keys()) if candidates else list(Candidate.__annotations__.keys()))
        writer.writeheader()
        for c in candidates:
            writer.writerow(asdict(c))
    print(f"json={json_path}")
    print(f"csv={csv_path}")
    print(f"latest={latest_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Scan public Polymarket data for paper edge candidates.")
    ap.add_argument("--max-markets", type=int, default=80)
    ap.add_argument("--max-books", type=int, default=120)
    ap.add_argument("--sleep", type=float, default=0.03)
    ap.add_argument("--outdir", type=Path, default=Path("/data/workspace/polymarket-research/reports"))
    args = ap.parse_args()
    markets = fetch_markets(args.max_markets)
    candidates, stats = scan(markets, max_books=args.max_books, sleep_s=args.sleep)
    candidates.sort(key=lambda c: {"high": 0, "medium": 1, "low": 2}.get(c.severity, 3))
    print(json.dumps(stats, indent=2))
    print("\nTop candidates:")
    for c in candidates[:20]:
        print(f"[{c.severity}] {c.kind} | {c.slug} | {c.outcomes} | {c.values} | {c.note[:120]}")
    write_outputs(candidates, stats, args.outdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
