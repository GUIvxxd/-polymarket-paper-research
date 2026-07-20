#!/usr/bin/env python3
"""Quiet proof-watchdog wrapper for xtracker paper entries.

Runs the proof tracker and prints only when new paper entries resolve or the readiness
threshold is reached. Empty stdout means no proof update.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path("/data/workspace/polymarket-research")
TRACKER = ROOT / "xtracker_paper_proof_tracker.py"
REPORT = ROOT / "reports" / "xtracker_paper_proof_latest.json"
STATE = ROOT / "data" / "xtracker_paper_proof_watchdog_state.json"
READY_RESOLVED = 20


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False))


def main() -> int:
    subprocess.run(
        [sys.executable, str(TRACKER)],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180,
        check=True,
    )
    payload = load_json(REPORT, {})
    summary = payload.get("summary") or {}
    state = load_json(STATE, {})
    prev_resolved = int(state.get("resolved") or 0)
    resolved = int(summary.get("resolved") or 0)
    wins = int(summary.get("wins") or 0)
    losses = int(summary.get("losses") or 0)
    avg_roi = summary.get("avg_roi_on_yes_cost")
    ready = resolved >= READY_RESOLVED and avg_roi is not None and float(avg_roi) > 0 and wins > losses
    was_ready = bool(state.get("ready"))
    save_json(STATE, {"resolved": resolved, "wins": wins, "losses": losses, "avg_roi_on_yes_cost": avg_roi, "ready": ready})

    if resolved == prev_resolved and not (ready and not was_ready):
        return 0

    print("Polymarket xtracker paper-proof update")
    print(f"resolved={resolved} wins={wins} losses={losses} avg_roi_on_yes_cost={avg_roi}")
    print(f"report={REPORT}")
    if ready and not was_ready:
        print("READINESS THRESHOLD REACHED: at least 20 resolved paper entries with positive average ROI and more wins than losses.")
    else:
        print("Not ready yet: keep collecting resolved paper entries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
