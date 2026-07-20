#!/usr/bin/env python3
"""Quiet Polymarket paper-edge watchdog.

Runs public-data scanner and prints only materially actionable paper candidates.
Cron no_agent=True can deliver non-empty stdout as an alert.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path("/data/workspace/polymarket-research")
SCANNER = ROOT / "strategy_scanner.py"
REPORTS = ROOT / "reports"
STATE = ROOT / "data" / "edge_watchdog_seen.json"
ACTIONABLE = {
    "buy_all_outcomes_arbitrage",
    "sell_all_or_mint_arbitrage",
    "crossed_book",
}


def key(c: dict) -> str:
    raw = "|".join([
        str(c.get("strategy", "")),
        str(c.get("condition_id", "")),
        str(c.get("outcomes", "")),
        str(c.get("values", "")),
    ])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def load_seen() -> set[str]:
    try:
        return set(json.loads(STATE.read_text()).get("seen", []))
    except Exception:
        return set()


def save_seen(seen: set[str]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"seen": sorted(seen)[-1000:]}, indent=2))


def main() -> int:
    REPORTS.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(SCANNER),
        "--max-markets",
        "700",
        "--max-book-markets",
        "260",
        "--sleep",
        "0.02",
        "--outdir",
        str(REPORTS),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=420)
    payload = json.loads((REPORTS / "strategy_scan_latest.json").read_text())
    candidates = payload.get("candidates", [])
    actionable = [
        c for c in candidates
        if c.get("strategy") in ACTIONABLE and int(c.get("executable_score") or 0) >= 2
    ]
    seen = load_seen()
    fresh = []
    for c in actionable:
        k = key(c)
        if k not in seen:
            fresh.append(c)
            seen.add(k)
    save_seen(seen)
    if not fresh:
        return 0

    stats = payload.get("stats", {})
    print("Polymarket paper-edge watchdog found actionable public-data candidates")
    print(f"scan_time={stats.get('retrieved_at_utc')} markets={stats.get('markets_scanned')} book_markets={stats.get('book_markets_scanned')}")
    for c in fresh[:10]:
        print()
        print(f"{c.get('severity')} | {c.get('strategy')} | score={c.get('executable_score')}")
        print(f"market={c.get('slug')}")
        print(f"outcomes={c.get('outcomes')}")
        print(f"values={c.get('values')}")
        print(f"note={c.get('note')}")
    print()
    print(f"full_report={REPORTS / 'strategy_scan_latest.json'}")
    print("Paper-only alert: validate freshness/depth manually before considering any real-money action.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
