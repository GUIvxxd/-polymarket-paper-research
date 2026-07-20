#!/usr/bin/env python3
"""Quiet watchdog for the active xtracker rebalance/exit paper strategy.

This is the active X paper-trading proof path. It:
1. Refreshes final settled counts using the legacy proof tracker as an internal resolver.
2. Replays tweet snapshots through the rebalance/exit strategy.
3. Updates the rebalance report/CSV/XLSX.
4. Prints only when the rebalance summary changes.

Public-data only: no X API, no wallet, no live orders.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path("/data/workspace/polymarket-research")
PROOF_TRACKER = ROOT / "xtracker_paper_proof_tracker.py"
REBALANCE_LEDGER = ROOT / "xtracker_paper_rebalance_ledger.py"
SUMMARY = ROOT / "reports" / "xtracker_rebalance_paper_summary_latest.json"
STATE = ROOT / "data" / "xtracker_rebalance_paper_watchdog_state.json"


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False))


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=240,
        check=True,
    )


def state_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    rebalance = payload.get("rebalance_summary") or {}
    return {
        "generated_at": payload.get("generated_at"),
        "snapshot_records": payload.get("snapshot_records"),
        "closed_trades": rebalance.get("closed_trades"),
        "wins": rebalance.get("wins"),
        "losses": rebalance.get("losses"),
        "breakeven": rebalance.get("breakeven"),
        "win_rate": rebalance.get("win_rate"),
        "paper_pnl": rebalance.get("paper_pnl"),
        "avg_roi": rebalance.get("avg_roi"),
        "median_roi": rebalance.get("median_roi"),
        "positive_exits": rebalance.get("positive_exits"),
        "negative_exits": rebalance.get("negative_exits"),
        "open_positions": payload.get("open_positions"),
        "summary_md": (payload.get("files") or {}).get("summary_md"),
        "summary_json": str(SUMMARY),
    }


def changed(prev: dict[str, Any], cur: dict[str, Any]) -> bool:
    keys = [
        "snapshot_records",
        "closed_trades",
        "wins",
        "losses",
        "breakeven",
        "win_rate",
        "paper_pnl",
        "avg_roi",
        "positive_exits",
        "negative_exits",
        "open_positions",
    ]
    return any(prev.get(k) != cur.get(k) for k in keys)


def main() -> int:
    # Internal final-count refresh. Suppress its old hold-to-settlement report output;
    # the active scheduled report is the rebalance summary below.
    run([sys.executable, str(PROOF_TRACKER)])
    run([sys.executable, str(REBALANCE_LEDGER)])

    payload = load_json(SUMMARY, {})
    if not payload:
        print(f"xtracker rebalance watchdog error: missing summary {SUMMARY}")
        return 1

    cur = state_from_payload(payload)
    prev = load_json(STATE, {})
    save_json(STATE, cur)

    if not changed(prev, cur):
        return 0

    print("Polymarket xtracker REBALANCE paper update")
    print(
        "closed={closed_trades} wins={wins} losses={losses} breakeven={breakeven} "
        "win_rate={win_rate} paper_pnl={paper_pnl} avg_roi={avg_roi} "
        "positive_exits={positive_exits} negative_exits={negative_exits} open={open_positions}".format(**cur)
    )
    print(f"report={ROOT / 'reports' / 'xtracker_rebalance_paper_summary_latest.md'}")
    print("Paper-only: no wallet, no private keys, no live orders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
