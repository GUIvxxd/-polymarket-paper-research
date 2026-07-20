from __future__ import annotations

import argparse
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .audit import audit_smoke
from .collector import run_rolling, run_smoke


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper-only Polymarket both-sides collector spike")
    sub = parser.add_subparsers(dest="command", required=True)
    smoke = sub.add_parser("smoke")
    smoke.add_argument("--duration-seconds", type=int, default=600)
    smoke.add_argument("--reconnect-after-seconds", type=int, default=90)
    smoke.add_argument("--fresh-ms", type=int, default=1000)
    smoke.add_argument("--output-root", type=Path, default=Path("reports/both_sides_spike"))
    rolling = sub.add_parser("rolling")
    rolling.add_argument("--duration-seconds", type=int, default=86_400)
    rolling.add_argument("--reconnect-after-seconds", type=int, default=90)
    rolling.add_argument("--fresh-ms", type=int, default=1000)
    rolling.add_argument("--discovery-interval-seconds", type=int, default=300)
    rolling.add_argument("--prestart-lead-seconds", type=int, default=30)
    rolling.add_argument("--durability-window-ms", type=int, default=200)
    rolling.add_argument("--compression", choices=("zstd", "zlib"), default="zstd")
    rolling.add_argument("--compression-level", type=int, default=12)
    rolling.add_argument("--disk-check-interval-seconds", type=int, default=30)
    rolling.add_argument("--minimum-free-bytes", type=int, default=1_000_000_000)
    rolling.add_argument("--output-root", type=Path, default=Path("reports/both_sides_spike/rolling"))
    audit = sub.add_parser("audit")
    audit.add_argument("manifest", type=Path)
    args = parser.parse_args()
    if args.command == "smoke":
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run_dir = args.output_root / f"smoke_{stamp}"
        manifest = run_smoke(
            run_dir,
            duration_seconds=args.duration_seconds,
            fresh_ms=args.fresh_ms,
            reconnect_after_seconds=args.reconnect_after_seconds,
        )
        json_path, md_path, result = audit_smoke(manifest)
        print(json.dumps({"manifest": str(manifest), "json_report": str(json_path), "markdown_report": str(md_path), "verdict": result["verdict"], "failed_gates": result["failed_gates"], "launch_24h_authorized": result["launch_24h_authorized"], "launch_blockers": result["launch_blockers"]}, indent=2))
        return 0 if result["verdict"] == "PASS" else 2
    if args.command == "rolling":
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run_dir = args.output_root / f"rolling_{stamp}_{uuid.uuid4().hex[:8]}"
        manifest = run_rolling(
            run_dir,
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
        result = json.loads(manifest.read_text())
        terminal_reason = result.get("terminal_reason")
        print(json.dumps({
            "manifest": str(manifest),
            "raw_log": result.get("raw_log"),
            "collector_run_id": result.get("collector_run_id"),
            "terminal_reason": terminal_reason,
            "controlled_stop": result.get("controlled_stop"),
            "final_chain_sha256": (result.get("terminal") or {}).get("final_chain_sha256"),
            "note": "No replay or economic calculations were run automatically.",
        }, indent=2))
        return 0 if terminal_reason == "collector_deadline" else 3
    json_path, md_path, result = audit_smoke(args.manifest)
    print(json.dumps({"json_report": str(json_path), "markdown_report": str(md_path), "verdict": result["verdict"], "failed_gates": result["failed_gates"], "launch_24h_authorized": result["launch_24h_authorized"], "launch_blockers": result["launch_blockers"]}, indent=2))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
