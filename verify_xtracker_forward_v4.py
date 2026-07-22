#!/usr/bin/env python3
"""Fail-closed verifier for immutable X v4 forensic evidence."""
from __future__ import annotations

import argparse
import ast
import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import xtracker_forward_provenance as provenance


DEFAULT_ROOT = Path("/data/workspace/polymarket-research")
EXPECTED_FROZEN_CONSTANTS = {
    "MIN_ABSOLUTE_PROFIT_EXIT": 0.03,
    "MIN_RELATIVE_PROFIT_EXIT": 0.20,
    "FAIR_COLLAPSE_THRESHOLD": 0.20,
    "STALE_BID_EDGE": 0.10,
    "BETTER_BUCKET_EDGE_DELTA": 0.10,
    "REBALANCE_MIN_EDGE": 0.50,
    "REBALANCE_MIN_FAIR": 0.70,
    "REBALANCE_MAX_ASK": 0.25,
}


@dataclass(frozen=True)
class AuditPaths:
    root: Path
    output: Path
    lock: Path
    state: Path
    status: Path
    events: Path
    registry: Path
    ledger: Path
    audit_json: Path
    audit_markdown: Path


def paths_for(root: Path = DEFAULT_ROOT) -> AuditPaths:
    output = root / "reports/xtracker_forward_validation/v4"
    return AuditPaths(
        root=root,
        output=output,
        lock=root / "config/xtracker_forward_validation_v4.lock.json",
        state=output / "state.json",
        status=output / "status.json",
        events=output / "evidence_events.jsonl",
        registry=output / "opportunity_registry.jsonl",
        ledger=output / "independent_event_ledger.csv",
        audit_json=output / "audit_latest.json",
        audit_markdown=output / "audit_latest.md",
    )


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def frozen_constants(path: Path) -> dict[str, Any]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id in EXPECTED_FROZEN_CONSTANTS:
            values[target.id] = ast.literal_eval(node.value)
    return values


def raw_refs(value: Any, path: str = "") -> list[tuple[str, str, str]]:
    references: list[tuple[str, str, str]] = []
    if isinstance(value, dict):
        if value.get("raw_path") and value.get("sha256"):
            references.append((value["raw_path"], value["sha256"], path))
        for key, item in value.items():
            if key.endswith("_path") and key not in {"raw_path", "settlement_proof_path"}:
                digest = value.get(key[:-5] + "_sha256")
                if digest:
                    references.append((item, digest, path + "/" + key))
            references.extend(raw_refs(item, path + "/" + key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            references.extend(raw_refs(item, path + f"/{index}"))
    return references


def _map_runtime_path(root: Path, raw_path: str) -> Path:
    path = PurePosixPath(raw_path)
    workspace = PurePosixPath("/data/workspace/polymarket-research")
    if path.is_absolute() and path.is_relative_to(workspace):
        return root.joinpath(*path.relative_to(workspace).parts)
    if path.is_absolute() and root != DEFAULT_ROOT:
        return root / "external" / Path(*path.parts[1:])
    return Path(raw_path) if path.is_absolute() else root / Path(*path.parts)


def _load_json(path: Path, label: str, errors: list[str]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        errors.append(f"{label}:missing_or_invalid")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{label}:not_object")
        return {}
    return value


def verify_provenance_records(
    *,
    lock: dict[str, Any],
    registry: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    events: list[dict[str, Any]],
    state: dict[str, Any],
    status: dict[str, Any],
) -> list[str]:
    return provenance.verify_experiment_provenance(
        lock, registry, ledger, events, state, status, require_binding=False
    )


def audit_records(
    *,
    lock: dict[str, Any],
    registry: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    events: list[dict[str, Any]],
    state: dict[str, Any],
    status: dict[str, Any],
    verified_at: str | None = None,
) -> dict[str, Any]:
    errors = verify_provenance_records(
        lock=lock,
        registry=registry,
        ledger=ledger,
        events=events,
        state=state,
        status=status,
    )
    completed = {
        arm: len((state.get("completed_clusters") or {}).get(arm) or {})
        for arm in provenance.ARMS
    }
    return {
        "schema_version": "xtracker_forward_v4_audit_v2",
        "verified_at": verified_at or datetime.now(UTC).isoformat(),
        "ok": not errors,
        "errors": errors,
        "classification": "mixed_version_forensic_evidence" if errors else "homogeneous_v4_evidence",
        "activation_utc": lock.get("activation_utc"),
        "lock_sha256": lock.get("lock_sha256"),
        "registry_records": len(registry),
        "evidence_records": len(events),
        "completed_clusters_by_arm": completed,
        "promotion_gate_passed": False,
        "paper_only": status.get("paper_only"),
        "live_orders": status.get("live_orders"),
        "wallet_or_authentication_used": status.get("wallet_or_authentication_used"),
    }


def audit(
    root: Path = DEFAULT_ROOT,
    *,
    write_audit: bool = True,
    verified_at: str | None = None,
) -> dict[str, Any]:
    paths = paths_for(root)
    errors: list[str] = []
    lock = _load_json(paths.lock, "lock", errors)
    lock_body = {key: value for key, value in lock.items() if key != "lock_sha256"}
    if lock and provenance.sha_bytes(provenance.canonical(lock_body)) != lock.get("lock_sha256"):
        errors.append("lock_self_hash")
    for raw_path, expected in sorted((lock.get("locked_source_sha256") or {}).items()):
        source = _map_runtime_path(root, raw_path)
        if not source.is_file() or provenance.sha_file(source) != expected:
            errors.append(f"locked_source:{raw_path}")

    manifest = _map_runtime_path(root, str(lock.get("frozen_strategy_manifest_path") or ""))
    if not manifest.is_file() or provenance.sha_file(manifest) != lock.get("frozen_strategy_manifest_sha256"):
        errors.append("frozen_manifest_hash")
    else:
        frozen_source = manifest.parent / "workspace/xtracker_paper_rebalance_ledger.py"
        if not frozen_source.is_file() or frozen_constants(frozen_source) != EXPECTED_FROZEN_CONSTANTS:
            errors.append("frozen_constant_mismatch")

    registry, registry_head, registry_errors = provenance.read_and_verify_chain(paths.registry, "registry")
    events, event_head, event_errors = provenance.read_and_verify_chain(paths.events, "events")
    errors.extend(registry_errors)
    errors.extend(event_errors)
    state = _load_json(paths.state, "state", errors)
    status = _load_json(paths.status, "status", errors)
    try:
        with paths.ledger.open(newline="", encoding="utf-8") as handle:
            ledger = list(csv.DictReader(handle))
    except OSError:
        ledger = []
        errors.append("ledger:missing")

    errors.extend(verify_provenance_records(
        lock=lock,
        registry=registry,
        ledger=ledger,
        events=events,
        state=state,
        status=status,
    ))
    chains = state.get("chains") or {}
    if (chains.get("registry") or {}).get("last_hash") != registry_head:
        errors.append("registry_head")
    if (chains.get("events") or {}).get("last_hash") != event_head:
        errors.append("events_head")
    if status.get("chain_heads") != chains:
        errors.append("status_heads")

    try:
        activation = parse_time(str(lock["activation_utc"]))
    except Exception:
        activation = None
        errors.append("activation_utc:invalid")
    if activation is not None:
        for row in registry + events:
            try:
                if row.get("decision_time") and parse_time(row["decision_time"]) < activation:
                    errors.append(f"preactivation:{row.get('record_hash')}")
            except Exception:
                errors.append(f"decision_time:invalid:{row.get('record_hash')}")

    references: list[tuple[str, str, str]] = []
    for row in events:
        references.extend(raw_refs(row))
    seen: set[tuple[str, str]] = set()
    for raw_path, expected, location in references:
        key = (raw_path, expected)
        if key in seen:
            continue
        seen.add(key)
        artifact = _map_runtime_path(root, raw_path)
        if not artifact.is_file() or provenance.sha_file(artifact) != expected:
            errors.append(f"raw:{location}:{raw_path}")

    entries = [row for row in events if row.get("record_type") == "ARM_ENTRY_EVALUATION"]
    for row in (item for item in entries if item.get("execution_evidence_eligible") is True):
        if row.get("decision") != "PAPER_ENTRY" or not (row.get("execution") or {}).get("complete"):
            errors.append(f"eligible_entry_shape:{row.get('record_hash')}")
        if any((row.get(section) or {}).get("problems") for section in (
            "decision_evidence", "fee_metadata", "fill_book"
        )):
            errors.append(f"eligible_entry_problems:{row.get('record_hash')}")
        try:
            delay = (
                parse_time(row["fill_book"]["request_started_at"]) - parse_time(row["decision_time"])
            ).total_seconds() * 1000
            if delay + 1e-6 < float(row["paper_order_latency_ms"]):
                errors.append(f"eligible_entry_latency:{row.get('record_hash')}")
        except Exception:
            errors.append(f"eligible_entry_latency_unverifiable:{row.get('record_hash')}")

    record_result = audit_records(
        lock=lock,
        registry=registry,
        ledger=ledger,
        events=events,
        state=state,
        status=status,
        verified_at=verified_at,
    )
    result = {
        **record_result,
        "ok": not errors,
        "errors": sorted(set(errors)),
        "classification": "mixed_version_forensic_evidence" if errors else "homogeneous_v4_evidence",
        "entry_evaluations": len(entries),
        "unique_raw_references_verified": len(seen),
        "chain_heads": chains,
    }
    if write_audit:
        paths.output.mkdir(parents=True, exist_ok=True)
        paths.audit_json.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        paths.audit_markdown.write_text(
            "\n".join([
                "# X forward v4 forensic audit",
                "",
                f"- Result: **{'PASS' if result['ok'] else 'FAIL'}**",
                f"- Classification: `{result['classification']}`",
                f"- Registered opportunities: `{len(registry)}`",
                f"- Evidence records: `{len(events)}`",
                f"- Completed clusters: `{result['completed_clusters_by_arm']}`",
                "- Promotion remains false; interim P&L is hidden.",
                "- Paper only; zero live orders and no wallet/authentication are required.",
                "",
            ]),
            encoding="utf-8",
        )
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify immutable X v4 forensic evidence")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--no-write", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = audit(args.root, write_audit=not args.no_write)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
