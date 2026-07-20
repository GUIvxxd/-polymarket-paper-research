#!/usr/bin/env python3
"""Parse Polymarket's public Tweet Markets page.

This avoids X recent-search credits. It extracts public event/market odds embedded in
https://polymarket.com/predictions/tweets-markets.

Paper/research only: no auth, no orders, no scraping of X itself.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

PAGE = "https://polymarket.com/predictions/tweets-markets"
UA = "Mozilla/5.0 Hermes-Polymarket-TweetPage-Parser/0.1"

EVENT_START = re.compile(
    r'\{"id":"(?P<id>\d+)","title":"(?P<title>[^"]+)","slug":"(?P<slug>[^"]+)"'
)
MARKET_RE = re.compile(
    r'\{"id":"(?P<id>\d+)","slug":"(?P<slug>[^"]+)","question":"(?P<question>[^"]+)",'
    r'"outcomes":\[(?P<outcomes>.*?)\],"outcomePrices":\[(?P<prices>.*?)\],"groupItemTitle":"(?P<group>[^"]*)",'
    r'"active":(?P<active>true|false),"closed":(?P<closed>true|false),"archived":(?P<archived>true|false)'
    r'(?:,"bestAsk":(?P<best_ask>[0-9.]+))?'
    r'(?:,"bestBid":(?P<best_bid>[0-9.]+))?'
    r'(?:,"lastTradePrice":(?P<last_trade>[0-9.]+))?'
    r'(?:,"spread":(?P<spread>[0-9.]+))?'
)


@dataclass
class EventRow:
    event_id: str
    title: str
    slug: str
    volume: float | None
    volume24hr: float | None
    liquidity: float | None
    end_date: str | None
    hours_to_end: float | None
    market_count: int
    leading_group: str | None
    leading_yes: float | None
    prices: str
    notes: str


def fnum(x: str | None) -> float | None:
    if not x:
        return None
    try:
        return float(x)
    except ValueError:
        return None


def parse_quoted_list(raw: str) -> list[str]:
    return [html.unescape(x) for x in re.findall(r'"([^"]*)"', raw)]


def hours_to_end(end: str | None) -> float | None:
    if not end:
        return None
    try:
        dt = datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None
    return (dt - datetime.now(UTC)).total_seconds() / 3600


def extract_metric(block: str, name: str) -> float | None:
    m = re.search(rf'"{name}":([0-9.]+)', block)
    return fnum(m.group(1)) if m else None


def extract_str(block: str, name: str) -> str | None:
    m = re.search(rf'"{name}":"([^"]+)"', block)
    return html.unescape(m.group(1)) if m else None


def parse_page(text: str) -> list[EventRow]:
    # The Next.js payload is embedded as escaped JSON inside JS strings. Normalize
    # the two common escaped-quote forms so regexes can operate on JSON-like text.
    text = text.replace('\\\\"', '"').replace('\\"', '"')
    starts = list(EVENT_START.finditer(text))
    rows: list[EventRow] = []
    for i, m in enumerate(starts):
        title = html.unescape(m.group('title'))
        slug = html.unescape(m.group('slug'))
        if not (
            'tweet' in title.lower()
            or 'post' in title.lower()
            or 'truth social' in title.lower()
            or 'tweet' in slug.lower()
            or 'truth-social' in slug.lower()
        ):
            continue
        end_pos = starts[i + 1].start() if i + 1 < len(starts) else min(len(text), m.start() + 20000)
        block = text[m.start():end_pos]
        markets = []
        for mm in MARKET_RE.finditer(block):
            outcomes = parse_quoted_list(mm.group('outcomes'))
            prices_s = parse_quoted_list(mm.group('prices'))
            yes = fnum(prices_s[0]) if prices_s else None
            markets.append({
                'group': html.unescape(mm.group('group')),
                'question': html.unescape(mm.group('question')),
                'yes': yes,
                'best_bid': fnum(mm.group('best_bid')),
                'best_ask': fnum(mm.group('best_ask')),
                'spread': fnum(mm.group('spread')),
                'active': mm.group('active') == 'true',
                'closed': mm.group('closed') == 'true',
            })
        if not markets:
            continue
        leading = max(markets, key=lambda x: x['yes'] if x['yes'] is not None else -1)
        end = extract_str(block, 'endDate')
        hte = hours_to_end(end)
        notes = []
        if hte is not None and 0 < hte < 36:
            notes.append('near_expiry_count_model_candidate')
        if leading.get('yes') is not None and (leading['yes'] >= 0.85 or leading['yes'] <= 0.15):
            notes.append('extreme_probability_check_actual_count')
        # Count market distribution anomalies: if all yes prices sum far from 1 in a count range event.
        yes_prices = [x['yes'] for x in markets if x['yes'] is not None]
        if len(yes_prices) >= 3:
            s = sum(yes_prices)
            if s < 0.95 or s > 1.05:
                notes.append(f'yes_sum={s:.3f}_check_package_pricing')
        rows.append(EventRow(
            event_id=m.group('id'),
            title=title,
            slug=slug,
            volume=extract_metric(block, 'volume'),
            volume24hr=extract_metric(block, 'volume24hr'),
            liquidity=extract_metric(block, 'liquidity'),
            end_date=end,
            hours_to_end=hte,
            market_count=len(markets),
            leading_group=leading.get('group'),
            leading_yes=leading.get('yes'),
            prices=' | '.join(f"{x['group']}={x['yes']:.3f}" for x in markets if x['yes'] is not None),
            notes=', '.join(notes),
        ))
    # Deduplicate by slug; embedded page may repeat cards/nav chunks.
    by_slug = {r.slug: r for r in rows}
    return sorted(by_slug.values(), key=lambda r: ((r.hours_to_end if r.hours_to_end is not None and r.hours_to_end > 0 else 10**9), -(r.volume24hr or 0)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--outdir', type=Path, default=Path('/data/workspace/polymarket-research/reports'))
    args = ap.parse_args()
    req = Request(PAGE, headers={'User-Agent': UA})
    text = urlopen(req, timeout=30).read().decode('utf-8', 'replace')
    rows = parse_page(text)
    args.outdir.mkdir(parents=True, exist_ok=True)
    payload = {
        'retrieved_at_utc': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        'source': PAGE,
        'events_found': len(rows),
        'x_api_recent_search_status': 'blocked: 402 credits depleted in this Hermes environment',
        'events': [asdict(r) for r in rows],
    }
    latest = args.outdir / 'tweet_page_events_latest.json'
    latest.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    csv_path = args.outdir / 'tweet_page_events_latest.csv'
    with csv_path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(EventRow.__annotations__.keys()))
        writer.writeheader()
        for r in rows:
            writer.writerow(asdict(r))
    print(json.dumps({k: payload[k] for k in ['retrieved_at_utc','events_found','x_api_recent_search_status']}, indent=2))
    for r in rows[:30]:
        h = '?' if r.hours_to_end is None else f'{r.hours_to_end:.1f}h'
        print(f"{h} | {r.title} | lead={r.leading_group}@{r.leading_yes} | vol24={r.volume24hr} | notes={r.notes}")
    print(f'latest={latest}')
    print(f'csv={csv_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
