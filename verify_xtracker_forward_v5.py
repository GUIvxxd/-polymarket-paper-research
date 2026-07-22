#!/usr/bin/env python3
"""Verifier for a single-identity, paper-only X v5 forward experiment."""
from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import xtracker_forward_provenance as provenance
import xtracker_forward_v5 as v5


def _load_object(path: Path, label: str, errors: list[str]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        errors.append(f"{label}:missing_or_invalid")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{label}:not_object")
        return {}
    return value


def _raw_refs(value: Any) -> list[tuple[str, str]]:
    references: list[tuple[str, str]] = []
    if isinstance(value, dict):
        if value.get("raw_path") and value.get("sha256"):
            references.append((str(value["raw_path"]), str(value["sha256"])))
        for key, item in value.items():
            if key.endswith("_path") and key not in {"raw_path", "settlement_proof_path"}:
                digest = value.get(key[:-5] + "_sha256")
                if digest:
                    references.append((str(item), str(digest)))
            references.extend(_raw_refs(item))
    elif isinstance(value, list):
        for item in value:
            references.extend(_raw_refs(item))
    return references


def audit(
    root: Path = v5.DEFAULT_ROOT,
    *,
    write_audit: bool = True,
    verified_at: str | None = None,
) -> dict[str, Any]:
    paths = v5.paths_for(root)
    errors: list[str] = []
    lock = _load_object(paths.lock, "lock", errors)
    protocol = _load_object(paths.protocol, "protocol", errors)
    state = _load_object(paths.state, "state", errors)
    status = _load_object(paths.status, "status", errors)
    activation_manifest = _load_object(paths.activation_manifest, "activation_manifest", errors)
    errors.extend(provenance.verify_lock(root, lock))
    identity = provenance.active_identity(lock)
    if protocol.get("protocol_id") != identity.get("protocol_id"):
        errors.append("protocol:protocol_id:mismatch")
    errors.extend(provenance.verify_identity_rows(
        [activation_manifest], identity, "activation_manifest"
    ))

    registry, registry_head, registry_errors = provenance.read_and_verify_chain(paths.registry, "registry")
    events, event_head, event_errors = provenance.read_and_verify_chain(paths.events, "events")
    errors.extend(registry_errors)
    errors.extend(event_errors)
    try:
        with paths.ledger.open(newline="", encoding="utf-8") as handle:
            ledger = list(csv.DictReader(handle))
    except OSError:
        ledger = []
        errors.append("ledger:missing")

    errors.extend(provenance.verify_experiment_provenance(
        lock, registry, ledger, events, state, status, require_binding=True
    ))
    chains = state.get("chains") or {}
    if (chains.get("registry") or {}).get("last_hash") != registry_head:
        errors.append("state:registry_chain_head:mismatch")
    if (chains.get("events") or {}).get("last_hash") != event_head:
        errors.append("state:events_chain_head:mismatch")
    if status.get("chain_heads") != chains:
        errors.append("status:chain_heads:mismatch")

    raw_verified = 0
    seen_raw: set[tuple[str, str]] = set()
    for raw_path, expected in _raw_refs(events):
        key = (raw_path, expected)
        if key in seen_raw:
            continue
        seen_raw.add(key)
        path = Path(raw_path)
        if not path.is_absolute():
            path = root / path
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError:
            errors.append(f"raw:unsafe_path:{raw_path}")
            continue
        if not path.is_file() or provenance.sha_file(path) != expected:
            errors.append(f"raw:hash_or_path:{raw_path}")
        else:
            raw_verified += 1

    completed = {
        arm: len((state.get("completed_clusters") or {}).get(arm) or {})
        for arm in provenance.ARMS
    }
    result = {
        "schema_version": "xtracker_forward_v5_audit_v1",
        "verified_at": verified_at or datetime.now(UTC).isoformat(),
        "ok": not errors,
        "errors": sorted(set(errors)),
        "protocol_id": identity.get("protocol_id"),
        "protocol_sha256": identity.get("protocol_sha256"),
        "baseline_lock_sha256": identity.get("baseline_lock_sha256"),
        "activation_utc": lock.get("activation_utc"),
        "registry_records": len(registry),
        "evidence_records": len(events),
        "completed_clusters_by_arm": completed,
        "promotion_gate_passed": False,
        "raw_references_verified": raw_verified,
        "paper_only": status.get("paper_only"),
        "live_orders": status.get("live_orders"),
        "wallet_or_authentication_used": status.get("wallet_or_authentication_used"),
        "aggregate_pnl_hidden_until_fixed_end": status.get("aggregate_pnl_hidden_until_fixed_end"),
        "chain_heads": chains,
    }
    if write_audit:
        paths.audit_json.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        paths.audit_markdown.write_text(
            "\n".join([
                "# X forward v5 audit",
                "",
                f"- Result: **{'PASS' if result['ok'] else 'FAIL'}**",
                f"- Registered opportunities: `{len(registry)}`",
                f"- Evidence records: `{len(events)}`",
                f"- Completed clusters: `{completed}`",
                "- Promotion remains false; interim P&L is hidden.",
                "- Paper only; zero live orders and no wallet/authentication are required.",
                "",
            ]),
            encoding="utf-8",
        )
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify X v5 forward evidence")
    parser.add_argument("--root", type=Path, default=v5.DEFAULT_ROOT)
    parser.add_argument("--no-write", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = audit(args.root, write_audit=not args.no_write)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
