#!/usr/bin/env python3
"""Exact-directory runner for a supervised, public-data-only collector run."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from both_sides_spike.collector import run_rolling


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--start-gate", type=Path)
    parser.add_argument("--start-gate-timeout-seconds", type=int, default=60)
    parser.add_argument("--duration-seconds", type=int, required=True)
    parser.add_argument("--fresh-ms", type=int, default=1000)
    parser.add_argument("--reconnect-after-seconds", type=int, default=90)
    parser.add_argument("--discovery-interval-seconds", type=int, default=300)
    parser.add_argument("--prestart-lead-seconds", type=int, default=30)
    parser.add_argument("--durability-window-ms", type=int, default=200)
    parser.add_argument("--compression", choices=("zstd", "zlib"), default="zstd")
    parser.add_argument("--compression-level", type=int, default=12)
    parser.add_argument("--disk-check-interval-seconds", type=int, default=30)
    parser.add_argument("--minimum-free-bytes", type=int, default=1_000_000_000)
    args = parser.parse_args()

    if args.minimum_free_bytes < 1_000_000_000:
        raise SystemExit("minimum-free-bytes cannot be below 1,000,000,000")
    if args.start_gate:
        deadline = time.monotonic() + args.start_gate_timeout_seconds
        while not args.start_gate.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not args.start_gate.exists():
            raise SystemExit("supervisor start gate was not armed before timeout")
    manifest = run_rolling(
        args.run_dir,
        duration_seconds=args.duration_seconds,
        fresh_ms=args.fresh_ms,
        reconnect_after_seconds=args.reconnect_after_seconds,
        discovery_interval_seconds=args.discovery_interval_seconds,
        prestart_lead_seconds=args.prestart_lead_seconds,
        durability_window_ms=args.durability_window_ms,
        compression=args.compression,
        compression_level=args.compression_level,
        disk_check_interval_seconds=args.disk_check_interval_seconds,
        minimum_free_bytes=args.minimum_free_bytes,
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    print(json.dumps({
        "manifest": str(manifest.resolve()),
        "collector_run_id": payload.get("collector_run_id"),
        "terminal_reason": payload.get("terminal_reason"),
        "controlled_stop": payload.get("controlled_stop"),
        "paper_only": (payload.get("configuration") or {}).get("paper_only"),
        "live_orders_enabled": (payload.get("configuration") or {}).get("live_orders_enabled"),
    }, sort_keys=True))
    return 0 if payload.get("terminal_reason") == "collector_deadline" else 3


if __name__ == "__main__":
    raise SystemExit(main())
