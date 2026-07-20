#!/usr/bin/env python3
"""Polymarket Tweet/X market signal scanner.

Paper/research only. No X search/scraping, no live orders.
Uses public Polymarket Gamma markets to find tweet/post-count or mention markets
and identify candidates for manual count-model research.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GAMMA = "https://gamma-api.polymarket.com"
UA = "Hermes-Polymarket-Tweet-Market-Scanner/0.1"

# Keep this tight: names like Trump/Elon alone are not enough, otherwise broad politics
# markets pollute the Tweet/X research set.
TWEET_PATTERNS = [
    re.compile(r"#\s*(tweets|posts)\b", re.I),
    re.compile(r"\b(tweet|tweets|posted|posts|post)\b", re.I),
    re.compile(r"\btruth social\b", re.I),
    re.compile(r"\bwhat will .*\b(post|tweet|mention)\b", re.I),
    re.compile(r"\bwill .*\b(tweet|post|mention)\b", re.I),
]
COUNT_RANGE = re.compile(r"(?:<\s*\d+|\d+\s*[-–]\s*\d+|\d+\+|\d+\s*or\s*more)", re.I)


@dataclass
class TweetMarket:
    slug: str
    question: str
    end_date: str | None
    seconds_to_end: float | None
    liquidity: float | None
    volume: float | None
    outcomes: str
    prices: str
    leading_outcome: str | None
    leading_price: float | None
    market_type: str
    note: str


def get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    if params:
        url += "?" + urlencode(params, doseq=True)
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def parse_list(x: Any) -> list[Any]:
    if isinstance(x, list):
        return x
    if isinstance(x, str) and x.strip():
        try:
            y = json.loads(x)
            return y if isinstance(y, list) else []
        except json.JSONDecodeError:
            return []
    return []


def to_float(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def seconds_to_end(end: str | None) -> float | None:
    if not end:
        return None
    try:
        dt = datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None
    return (dt - datetime.now(UTC)).total_seconds()


def fetch(max_markets: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    offset = 0
    while len(out) < max_markets:
        limit = min(100, max_markets - len(out))
        page = get_json(f"{GAMMA}/markets", {"active": "true", "closed": "false", "limit": limit, "offset": offset})
        if not isinstance(page, list) or not page:
            break
        out.extend([m for m in page if isinstance(m, dict)])
        if len(page) < limit:
            break
        offset += limit
    return out[:max_markets]


def classify(question: str, outcomes: list[str]) -> str:
    q = question.lower()
    if "#" in question and ("tweet" in q or "post" in q):
        return "post_count"
    if "what will" in q and ("post" in q or "tweet" in q):
        return "topic_mention"
    if any(COUNT_RANGE.search(o or "") for o in outcomes):
        return "count_range"
    if "truth social" in q:
        return "truth_social"
    return "twitter_related"


def scan(markets: list[dict[str, Any]]) -> list[TweetMarket]:
    rows: list[TweetMarket] = []
    for m in markets:
        q = str(m.get("question") or m.get("title") or "")
        slug = str(m.get("slug") or "")
        hay = f"{q} {slug}"
        outcomes = [str(x) for x in parse_list(m.get("outcomes"))]
        prices = [to_float(x) for x in parse_list(m.get("outcomePrices"))]
        if not any(p.search(hay) for p in TWEET_PATTERNS):
            continue
        leading = None
        leading_price = None
        if prices and len(prices) == len(outcomes):
            idx = max(range(len(prices)), key=lambda i: prices[i] if prices[i] is not None else -1)
            leading = outcomes[idx]
            leading_price = prices[idx]
        end = m.get("endDate") or m.get("endDateIso")
        secs = seconds_to_end(str(end) if end else None)
        mt = classify(q, outcomes)
        note_parts = []
        if mt in {"post_count", "count_range", "truth_social"}:
            note_parts.append("Needs independent current-count + rate model.")
        if secs is not None and 0 < secs < 36 * 3600:
            note_parts.append("Near expiry; count/rate edge may be testable quickly.")
        if leading_price is not None and (leading_price >= 0.85 or leading_price <= 0.15):
            note_parts.append("Extreme leading probability; check for stale count or completed threshold.")
        rows.append(TweetMarket(
            slug=slug,
            question=q,
            end_date=str(end) if end else None,
            seconds_to_end=secs,
            liquidity=to_float(m.get("liquidity") or m.get("liquidityNum") or m.get("liquidityClob")),
            volume=to_float(m.get("volume") or m.get("volumeNum")),
            outcomes=" | ".join(outcomes),
            prices=" | ".join("" if p is None else f"{p:.4f}" for p in prices),
            leading_outcome=leading,
            leading_price=leading_price,
            market_type=mt,
            note=" ".join(note_parts) or "Twitter/X-related market; needs external truth/count feed before paper signal.",
        ))
    rows.sort(key=lambda r: (r.seconds_to_end if r.seconds_to_end is not None and r.seconds_to_end > 0 else 10**12, -(r.volume or 0)))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-markets", type=int, default=1000)
    ap.add_argument("--outdir", type=Path, default=Path("/data/workspace/polymarket-research/reports"))
    args = ap.parse_args()
    markets = fetch(args.max_markets)
    rows = scan(markets)
    args.outdir.mkdir(parents=True, exist_ok=True)
    payload = {
        "retrieved_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "markets_scanned": len(markets),
        "tweet_markets_found": len(rows),
        "x_api_recent_search_status": "blocked: 402 credits depleted in this Hermes environment",
        "markets": [asdict(r) for r in rows],
    }
    latest = args.outdir / "tweet_market_scan_latest.json"
    latest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    csv_path = args.outdir / "tweet_market_scan_latest.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(TweetMarket.__annotations__.keys()))
        writer.writeheader()
        for r in rows:
            writer.writerow(asdict(r))
    print(json.dumps({k: payload[k] for k in ['retrieved_at_utc','markets_scanned','tweet_markets_found','x_api_recent_search_status']}, indent=2))
    for r in rows[:25]:
        hrs = None if r.seconds_to_end is None else r.seconds_to_end / 3600
        print(f"{r.market_type} | {hrs:.1f}h | {r.slug} | lead={r.leading_outcome}@{r.leading_price} | {r.note}" if hrs is not None else f"{r.market_type} | ?h | {r.slug} | lead={r.leading_outcome}@{r.leading_price} | {r.note}")
    print(f"latest={latest}")
    print(f"csv={csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
