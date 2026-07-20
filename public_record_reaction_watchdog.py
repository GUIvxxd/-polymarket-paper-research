#!/usr/bin/env python3
"""Quiet wrapper for public_record_reaction_bot.py.

Runs the paper-only public-record scanner and prints only when fresh
medium/high signals appear. This is suitable for Hermes cron/no-agent delivery.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path("/data/workspace/polymarket-research")
SCANNER = ROOT / "public_record_reaction_bot.py"
SUMMARY = ROOT / "reports" / "public_record_reaction_summary_latest.json"


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def main() -> int:
    cmd = [
        sys.executable,
        str(SCANNER),
        "--sec-count",
        "120",
        "--award-days",
        "7",
        "--award-limit",
        "20",
        "--polymarket-limit",
        "250",
        "--min-score",
        "55",
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
    if proc.returncode != 0:
        print("public-record watchdog error")
        if proc.stdout.strip():
            print(proc.stdout.strip())
        if proc.stderr.strip():
            print(proc.stderr.strip())
        return proc.returncode

    summary = load_json(SUMMARY, {})
    top = summary.get("top_signals") or []
    fresh = [s for s in top if s.get("new_signal") and float(s.get("signal_score") or 0) >= 70]
    if not fresh:
        return 0

    print("PUBLIC-RECORD PAPER SIGNALS")
    print(
        f"new={summary.get('new_signals')} high={summary.get('high_priority_signals')} "
        f"ticker_matches={summary.get('ticker_matched_signals')} report={summary.get('files', {}).get('summary_md')}"
    )
    for signal in fresh[:8]:
        print(
            f"- {signal.get('priority')} score={signal.get('signal_score')} "
            f"{signal.get('source')} {signal.get('record_type')} "
            f"{signal.get('ticker') or ''} {signal.get('company')}: {signal.get('reason')}"
        )
        if signal.get("url"):
            print(f"  source={signal.get('url')}")
    print("Paper-only: no broker, no wallet, no private keys, no live orders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
