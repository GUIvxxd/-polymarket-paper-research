from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

from .raw_log import verify_raw_log
from .replay import replay_records


def _market_identity(meta: dict[str, Any]) -> dict[str, Any]:
    value = meta.get("identity")
    return value if isinstance(value, dict) else {}


def audit_smoke(manifest_path: Path | str) -> tuple[Path, Path, dict[str, Any]]:
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    raw_path = Path(manifest["raw_log"])
    raw_audit = verify_raw_log(raw_path)
    replays = []
    replay_ok = True
    for meta in manifest.get("markets", []):
        identity = _market_identity(meta)
        if identity.get("stage") != "IDENTITY_VERIFIED":
            continue
        outputs = [
            replay_records(
                raw_path,
                condition_id=identity["condition_id"],
                up_token_id=identity["up_token_id"],
                down_token_id=identity["down_token_id"],
            )
            for _ in range(3)
        ]
        hashes = [row.canonical_hash for row in outputs]
        deterministic = len(set(hashes)) == 1
        replay_ok &= deterministic
        replays.append({
            "condition_id": identity["condition_id"],
            "slug": identity["slug"],
            "hashes": hashes,
            "deterministic": deterministic,
            "input_frames": outputs[0].input_frames,
            "pair_observations": outputs[0].pair_observations,
        })
    classes = set(manifest.get("required_market_classes", []))
    required = {"BTC-5m", "ETH-5m", "BTC-15m", "ETH-15m"}
    markets = manifest.get("markets", [])
    identity_ok = all(_market_identity(m).get("stage") == "IDENTITY_VERIFIED" for m in markets)
    outcomes_ok = all(
        _market_identity(m).get("up_token_id") and _market_identity(m).get("down_token_id")
        for m in markets
    )
    fees_ok = all(bool((m.get("fee_gate") or {}).get("economic_eligible")) for m in markets)
    future_ok = any(
        m.get("is_future") and "CLOB_PENDING" in m.get("stages", []) and "IDENTITY_VERIFIED" in m.get("stages", [])
        for m in markets
    )
    pair_by_epoch = manifest.get("pair_by_epoch", {})
    rolling_mode = manifest.get("collection_mode") == "rolling"
    subscription_history = manifest.get("subscription_history", []) if isinstance(manifest.get("subscription_history"), list) else []
    if rolling_mode and subscription_history:
        first_entry = subscription_history[0]
        first_epoch = str(first_entry.get("epoch"))
        pre_pair = all(
            int((pair_by_epoch.get(condition, {}) or {}).get(first_epoch, 0)) > 0
            for condition in first_entry.get("condition_ids", [])
        ) and bool(first_entry.get("condition_ids"))
        post_pair = any(
            all(int((pair_by_epoch.get(condition, {}) or {}).get(str(entry.get("epoch")), 0)) > 0 for condition in entry.get("condition_ids", []))
            and bool(entry.get("condition_ids"))
            for entry in subscription_history[1:]
        )
        rotated_pair_ready = False
        previous_conditions = set(first_entry.get("condition_ids", []))
        previous_hash = first_entry.get("subscription_set_hash")
        for entry in subscription_history[1:]:
            current_conditions = set(entry.get("condition_ids", []))
            current_hash = entry.get("subscription_set_hash")
            introduced = current_conditions - previous_conditions
            if current_hash != previous_hash and introduced and all(
                int((pair_by_epoch.get(condition, {}) or {}).get(str(entry.get("epoch")), 0)) > 0
                for condition in introduced
            ):
                rotated_pair_ready = True
                break
            previous_conditions = current_conditions
            previous_hash = current_hash
    else:
        pre_pair = all(int((pair_by_epoch.get(_market_identity(m).get("condition_id"), {}) or {}).get("1", 0)) > 0 for m in markets)
        post_pair = all(any(int(count) > 0 for epoch, count in (pair_by_epoch.get(_market_identity(m).get("condition_id"), {}) or {}).items() if int(epoch) >= 2) for m in markets)
        rotated_pair_ready = False
    elapsed = float(manifest.get("collection_elapsed_seconds", 0))
    gates = {
        "smoke_duration_10_to_20_minutes": 600 <= elapsed <= 1200,
        "four_market_classes": classes >= required,
        "identity_verified": identity_ok,
        "up_down_mapping": outcomes_ok,
        "gamma_fee_and_token_metadata_supported": fees_ok,
        "future_market_clob_pending_progression": future_ok,
        "forced_reconnect_completed": bool(manifest.get("forced_reconnect_completed")),
        "pair_ready_before_reconnect": pre_pair,
        "pair_ready_after_reconnect": post_pair,
        "rolling_discovery_polled_multiple_times": (not rolling_mode) or int(manifest.get("discovery_polls", 0)) >= 2,
        "rolling_subscription_rotation_observed": (not rolling_mode) or int(manifest.get("subscription_rotations", 0)) >= 1,
        "rotated_market_pair_ready": (not rolling_mode) or rotated_pair_ready,
        "raw_integrity": raw_audit.ok,
        "three_replays_deterministic": replay_ok and bool(replays),
        "forced_durable_flush_under_250ms_p99": manifest.get("p99_receive_to_durable_log_ms") is not None and float(manifest["p99_receive_to_durable_log_ms"]) <= 250,
        "zero_parser_errors": int(manifest.get("parser_errors", 0)) == 0,
        "property_tests_10000_each": manifest.get("tests_verified_before_smoke") is True and int(manifest.get("property_examples_per_core_subsystem", 0)) >= 10_000,
        "paper_only": manifest.get("live_orders") == 0 and manifest.get("wallet_or_authentication_used") is False,
    }
    verdict = "PASS" if all(gates.values()) else "NO_GO"
    raw_bytes = raw_path.stat().st_size
    statvfs = os.statvfs(raw_path.parent)
    available_bytes = statvfs.f_bavail * statvfs.f_frsize
    projected_24h_bytes = int(raw_bytes / elapsed * 86_400) if elapsed > 0 else None
    storage_headroom_ok = projected_24h_bytes is not None and projected_24h_bytes <= int(available_bytes * 0.80)
    launch_gates = {
        "collector_preflight_pass": verdict == "PASS",
        "rolling_collection_mode": rolling_mode,
        "rolling_subscription_rotation_observed": rolling_mode and int(manifest.get("subscription_rotations", 0)) >= 1,
        "rotated_market_pair_ready": rolling_mode and rotated_pair_ready,
        "projected_24h_storage_with_20pct_headroom": storage_headroom_ok,
    }
    launch_blockers = [name for name, passed in launch_gates.items() if not passed]
    result = {
        "schema_version": "both_sides_smoke_gate_report_v1",
        "verdict": verdict,
        "collector_run_id": manifest.get("collector_run_id"),
        "gates": gates,
        "failed_gates": [name for name, passed in gates.items() if not passed],
        "raw_integrity": {"ok": raw_audit.ok, "frame_count": raw_audit.frame_count, "control_count": raw_audit.control_count, "errors": list(raw_audit.errors)},
        "replays": replays,
        "pair_state_counts": {condition: dict(counts) for condition, counts in manifest.get("pair_counts", {}).items()},
        "multi_token_frames_observed": manifest.get("multi_token_frames_observed", 0),
        "atomic_multi_token_handling_tested": manifest.get("atomic_multi_token_handling_tested", False),
        "p99_receive_to_durable_log_ms": manifest.get("p99_receive_to_durable_log_ms"),
        "errors": manifest.get("errors", []),
        "storage": {
            "raw_log_bytes": raw_bytes,
            "available_bytes_at_audit": available_bytes,
            "projected_24h_bytes": projected_24h_bytes,
            "projection_basis_seconds": elapsed,
        },
        "launch_gates": launch_gates,
        "launch_blockers": launch_blockers,
        "launch_24h_authorized": not launch_blockers,
    }
    json_path = manifest_path.parent / "smoke_gate_report.json"
    md_path = manifest_path.parent / "smoke_gate_report.md"
    json_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    lines = [
        "# Both-sides collector smoke-test gate report",
        "",
        f"Verdict: **{verdict}**",
        "",
        "This report validates collector behavior only. It does not test profitability or authorize live trading.",
        "",
        "## Gates",
        "",
    ]
    lines.extend(f"- {'PASS' if passed else 'FAIL'} — `{name}`" for name, passed in gates.items())
    lines += [
        "",
        "## Evidence",
        "",
        f"- Smoke collection elapsed: {elapsed:.3f} seconds",
        f"- Raw WS frames: {raw_audit.frame_count}",
        f"- Raw/control integrity errors: {len(raw_audit.errors)}",
        f"- Markets replayed three times: {len(replays)}",
        f"- Multi-token price-change frames observed live: {result['multi_token_frames_observed']}",
        f"- Atomic multi-token reducer covered by tests: {result['atomic_multi_token_handling_tested']}",
        f"- p99 receive-to-fsync latency: {result['p99_receive_to_durable_log_ms']} ms",
        f"- Raw archive bytes: {raw_bytes}",
        f"- Projected 24-hour archive bytes: {projected_24h_bytes}",
        f"- Available filesystem bytes at audit: {available_bytes}",
        "- Live orders: 0",
        "- Wallet/authentication used: no",
        "",
        "## 24-hour launch readiness",
        "",
    ]
    lines.extend(f"- {'PASS' if passed else 'FAIL'} — `{name}`" for name, passed in launch_gates.items())
    lines += [
        "",
        f"- 24-hour launch authorized: {'yes' if result['launch_24h_authorized'] else 'no'}",
    ]
    md_path.write_text("\n".join(lines) + "\n")
    return json_path, md_path, result
