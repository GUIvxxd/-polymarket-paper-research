#!/usr/bin/env python3
"""Quiet watchdog wrapper for the multi-market Polymarket research scanner.

Default behavior is suitable for cron/script-only mode:
- runs the public-data scanner;
- updates reports;
- prints only when new actionable stock/price candidates appear;
- never uses authenticated trading endpoints.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path("/data/workspace/polymarket-research")
SCANNER = ROOT / "multi_market_research_bot.py"
STATE = ROOT / "data" / "multi_market_watchdog_state.json"
REPORT = ROOT / "reports" / "multi_market_research_latest.json"


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def candidate_key(candidate: dict[str, Any]) -> str:
    return "|".join(
        str(candidate.get(key) or "")
        for key in ("condition_id", "side", "outcome", "ticker", "threshold", "direction")
    )


def run_scanner(args: argparse.Namespace) -> None:
    cmd = [
        sys.executable,
        str(SCANNER),
        "--modules",
        "all",
        "--max-markets",
        str(args.max_markets),
        "--search-limit",
        str(args.search_limit),
        "--max-books",
        str(args.max_books),
        "--min-edge",
        str(args.min_edge),
        "--min-size",
        str(args.min_size),
        "--max-ask",
        str(args.max_ask),
    ]
    result = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=args.timeout)
    if result.returncode != 0:
        raise RuntimeError(f"scanner failed exit={result.returncode}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")


def build_alert(report: dict[str, Any], previous_seen: set[str]) -> tuple[str, set[str]]:
    stock = report.get("stock", {})
    candidates = stock.get("candidates", [])
    actionable = [c for c in candidates if c.get("actionable")]
    current_keys = {candidate_key(c) for c in actionable}
    new_candidates = [c for c in actionable if candidate_key(c) not in previous_seen]
    if not new_candidates:
        return "", current_keys

    lines = []
    lines.append("New Polymarket stock/price paper candidate(s) found")
    lines.append(f"Retrieved: {report.get('retrieved_at_utc')}")
    lines.append("Paper-only: no live orders, public data only.")
    lines.append("")
    for c in new_candidates[:10]:
        lines.append(fmt_candidate_line(c))
    lines.append("")
    lines.append(f"Report: {ROOT / 'reports' / 'multi_market_research_latest.md'}")
    return "\n".join(lines), current_keys


def fmt_candidate_line(c: dict[str, Any]) -> str:
    fair = c.get("fair_probability")
    fair_text = f"{fair:.3f}" if isinstance(fair, int | float) else str(fair)
    ask = c.get("ask")
    edge = c.get("edge")
    return (
        f"- {c.get('ticker')} {c.get('side')} | {c.get('question')}\n"
        f"  price={c.get('current_price')} threshold={c.get('direction')} {c.get('threshold')} "
        f"fair={fair_text} ask={ask} edge={edge}\n"
        f"  note={c.get('note')}"
    )


def build_status(report: dict[str, Any]) -> str:
    stock_stats = report.get("stock", {}).get("stats", {})
    news_stats = report.get("news", {}).get("stats", {})
    wallet_stats = report.get("wallet", {}).get("stats", {})
    return "\n".join(
        [
            f"Multi-market scanner refreshed: {report.get('retrieved_at_utc')}",
            "Paper-only; no live orders.",
            f"Stock: modeled={stock_stats.get('modeled_markets', 0)} actionable={stock_stats.get('actionable_candidates', 0)}",
            f"News watchlist: {news_stats.get('news_watchlist_candidates', 0)} candidates",
            f"Wallets: requested={wallet_stats.get('wallets_requested', 0)} scanned={wallet_stats.get('wallets_scanned', 0)}",
            f"Report: {ROOT / 'reports' / 'multi_market_research_latest.md'}",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-markets", type=int, default=250)
    parser.add_argument("--search-limit", type=int, default=25)
    parser.add_argument("--max-books", type=int, default=60)
    parser.add_argument("--min-edge", type=float, default=0.08)
    parser.add_argument("--min-size", type=float, default=5.0)
    parser.add_argument("--max-ask", type=float, default=0.85)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--status", action="store_true", help="Always print a short status summary.")
    args = parser.parse_args()

    state = load_json(STATE, {"seen_actionable_stock_keys": []})
    previous_seen = set(state.get("seen_actionable_stock_keys") or [])
    run_scanner(args)
    report = load_json(REPORT, {})
    alert, current_keys = build_alert(report, previous_seen)
    save_json(
        STATE,
        {
            "last_run_at": report.get("retrieved_at_utc"),
            "seen_actionable_stock_keys": sorted(previous_seen | current_keys),
            "latest_report": str(REPORT),
        },
    )
    if alert:
        print(alert)
    elif args.status:
        print(build_status(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
