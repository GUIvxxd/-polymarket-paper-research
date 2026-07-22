#!/usr/bin/env python3
"""Explicit activation and runtime provenance binding for a fresh X v5 sample."""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import xtracker_forward_provenance as provenance


DEFAULT_ROOT = Path("/data/workspace/polymarket-research")
ACTIVATION_CONFIRMATION = "CREATE_EMPTY_XTRACKER_V5"
TEMPLATE_PATH = Path("config/xtracker_forward_validation_v5.template.json")
REQUIRED_SOURCE_PATHS = (
    TEMPLATE_PATH,
    Path("xtracker_forward_capture.py"),
    Path("xtracker_forward_engine_v5.py"),
    Path("xtracker_forward_monitor_v4.py"),
    Path("xtracker_forward_provenance.py"),
    Path("xtracker_forward_v5.py"),
    Path("verify_xtracker_forward_v5.py"),
    Path("xtracker_tweet_depth_check.py"),
    Path("xtracker_tweet_watchdog.py"),
    Path("xtracker_paper_rebalance_ledger.py"),
)
ECONOMIC_SECTIONS = (
    "baseline",
    "registered_candidate_arms",
    "universe_and_independence",
    "causal_execution",
    "failure_accounting",
    "statistics_and_promotion",
)


class ActivationError(RuntimeError):
    pass


@dataclass(frozen=True)
class V5Paths:
    root: Path
    output: Path
    protocol: Path
    lock: Path
    state: Path
    status: Path
    registry: Path
    events: Path
    ledger: Path
    raw: Path
    activation_manifest: Path
    audit_json: Path
    audit_markdown: Path


def paths_for(root: Path = DEFAULT_ROOT) -> V5Paths:
    output = root / "reports/xtracker_forward_validation/v5"
    return V5Paths(
        root=root,
        output=output,
        protocol=output / "protocol.json",
        lock=output / "lock.json",
        state=output / "state.json",
        status=output / "status.json",
        registry=output / "opportunity_registry.jsonl",
        events=output / "evidence_events.jsonl",
        ledger=output / "independent_event_ledger.csv",
        raw=output / "raw",
        activation_manifest=output / "activation_manifest.json",
        audit_json=output / "audit_latest.json",
        audit_markdown=output / "audit_latest.md",
    )


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ActivationError("activation clock must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _protocol_id(value: datetime) -> str:
    stamp = value.astimezone(UTC).strftime("%Y%m%dT%H%M%S")
    milliseconds = value.microsecond // 1000
    return f"xtracker_forward_v5_{stamp}{milliseconds:03d}Z"


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ActivationError(f"{label} is missing or invalid: {path}") from exc
    if not isinstance(value, dict):
        raise ActivationError(f"{label} must be a JSON object: {path}")
    return value


def _validate_unchanged_economics(root: Path, template: dict[str, Any]) -> None:
    v4_protocol = _load_object(root / "config/xtracker_forward_validation_v4.json", "v4 protocol")
    for section in ECONOMIC_SECTIONS:
        if template.get(section) != v4_protocol.get(section):
            raise ActivationError(f"v5 template changes frozen economics: {section}")


def _zero_state(identity: dict[str, str], activation_utc: str) -> dict[str, Any]:
    return provenance.bind_identity(
        {
            "schema_version": "xtracker_forward_state_v5",
            "activation_utc": activation_utc,
            "paper_only": True,
            "seen_opportunities": {},
            "entered_lifecycles": {},
            "open_positions": {arm: {} for arm in provenance.ARMS},
            "completed_clusters": {arm: {} for arm in provenance.ARMS},
            "entry_evaluations": 0,
            "chains": {
                "registry": {"sequence": 0, "last_hash": provenance.ZERO_HASH},
                "events": {"sequence": 0, "last_hash": provenance.ZERO_HASH},
            },
        },
        identity,
    )


def _zero_status(identity: dict[str, str], activation_utc: str, chains: dict[str, Any]) -> dict[str, Any]:
    return provenance.bind_identity(
        {
            "schema_version": "xtracker_forward_status_v5",
            "activation_utc": activation_utc,
            "paper_only": True,
            "live_orders": 0,
            "wallet_or_authentication_used": False,
            "registered_opportunities": 0,
            "entry_evaluations": 0,
            "open_positions_by_arm": {arm: 0 for arm in provenance.ARMS},
            "executable_completed_clusters_by_arm": {arm: 0 for arm in provenance.ARMS},
            "net_capturable_completed_clusters_by_arm": {arm: 0 for arm in provenance.ARMS},
            "promotion_gate_passed": False,
            "minimum_completed_clusters_per_arm": 30,
            "aggregate_pnl_hidden_until_fixed_end": True,
            "public_record_net_capturable_markouts": 0,
            "note": "v5 activated with empty evidence; no prior rows imported",
            "chain_heads": chains,
        },
        identity,
    )


def _ledger_header_bytes() -> bytes:
    from io import StringIO

    buffer = StringIO(newline="")
    csv.DictWriter(buffer, fieldnames=provenance.LEDGER_FIELDS, lineterminator="\n").writeheader()
    return buffer.getvalue().encode()


def activate(
    root: Path = DEFAULT_ROOT,
    *,
    confirmation: str,
    deployment_wrapper: Path | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    if confirmation != ACTIVATION_CONFIRMATION:
        raise ActivationError(f"activation confirmation must equal {ACTIVATION_CONFIRMATION}")
    paths = paths_for(root)
    if paths.output.exists():
        raise ActivationError(f"v5 target is not empty or is already reserved: {paths.output}")
    if deployment_wrapper is None or not deployment_wrapper.is_absolute() or not deployment_wrapper.is_file():
        raise ActivationError("an existing absolute deployment wrapper path is required")
    for relative_path in REQUIRED_SOURCE_PATHS:
        if not (root / relative_path).is_file():
            raise ActivationError(f"required locked source is missing: {relative_path.as_posix()}")

    template = _load_object(root / TEMPLATE_PATH, "v5 protocol template")
    _validate_unchanged_economics(root, template)
    now = (clock or (lambda: datetime.now(UTC)))()
    activation_utc = _timestamp(now)
    protocol_id = _protocol_id(now)
    v4_lock = _load_object(root / "config/xtracker_forward_validation_v4.lock.json", "v4 lock")
    if activation_utc == v4_lock.get("activation_utc"):
        raise ActivationError("v5 activation timestamp must not reuse v4 activation")

    protocol = {
        **template,
        "schema_version": "xtracker_forward_validation_protocol_v5",
        "status": "ACTIVE_FROM_GENERATED_V5_LOCK_TIMESTAMP",
        "protocol_id": protocol_id,
    }
    protocol_bytes = _json_bytes(protocol)
    protocol_sha256 = provenance.sha_bytes(protocol_bytes)
    source_hashes = {
        relative_path.as_posix(): provenance.sha_file(root / relative_path)
        for relative_path in REQUIRED_SOURCE_PATHS
    }
    lock_body = {
        "schema_version": "xtracker_forward_validation_lock_v3",
        "protocol_id": protocol_id,
        "protocol_sha256": protocol_sha256,
        "protocol_path": "reports/xtracker_forward_validation/v5/protocol.json",
        "activation_utc": activation_utc,
        "amendment_rule": "substantive change requires v6 and resets the forward sample",
        "forensic_predecessor": {
            "protocol_id": v4_lock.get("protocol_id"),
            "lock_sha256": v4_lock.get("lock_sha256"),
            "promotion_rows_imported": 0,
        },
        "locked_source_sha256": source_hashes,
        "external_locked_source_sha256": {
            str(deployment_wrapper.resolve()): provenance.sha_file(deployment_wrapper),
        },
        "historical_rows_count": False,
        "v3_rows_count": False,
        "v4_rows_count": False,
        "paper_only": True,
        "live_orders_allowed": False,
        "wallet_or_authentication_allowed": False,
        "minimum_executable_net_capturable_clusters_per_arm": 30,
        "fixed_end": {
            "calendar_days": 180,
            "completed_independent_clusters": 100,
            "whichever_first": True,
        },
    }
    lock = {**lock_body, "lock_sha256": provenance.sha_bytes(provenance.canonical(lock_body))}
    identity = provenance.active_identity(lock)
    state = _zero_state(identity, activation_utc)
    status = _zero_status(identity, activation_utc, state["chains"])
    activation_manifest = provenance.bind_identity(
        {
            "schema_version": "xtracker_forward_v5_activation_manifest_v1",
            "activation_utc": activation_utc,
            "source_template_sha256": source_hashes[TEMPLATE_PATH.as_posix()],
            "zero_opportunities": True,
            "zero_completed_clusters": True,
            "imported_prior_rows": 0,
            "paper_only": True,
            "live_orders": 0,
            "wallet_or_authentication_used": False,
        },
        identity,
    )

    paths.output.parent.mkdir(parents=True, exist_ok=True)
    staging = paths.output.parent / f".v5-activation-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        (staging / "raw").mkdir()
        (staging / "protocol.json").write_bytes(protocol_bytes)
        (staging / "lock.json").write_bytes(_json_bytes(lock))
        (staging / "state.json").write_bytes(_json_bytes(state))
        (staging / "status.json").write_bytes(_json_bytes(status))
        (staging / "activation_manifest.json").write_bytes(_json_bytes(activation_manifest))
        (staging / "opportunity_registry.jsonl").write_bytes(b"")
        (staging / "evidence_events.jsonl").write_bytes(b"")
        (staging / "independent_event_ledger.csv").write_bytes(_ledger_header_bytes())
        for relative_path, expected in source_hashes.items():
            if provenance.sha_file(root / relative_path) != expected:
                raise ActivationError(f"locked source changed during activation: {relative_path}")
        os.replace(staging, paths.output)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    lock_errors = provenance.verify_lock(root, lock)
    if lock_errors:
        raise ActivationError("generated lock verification failed: " + ", ".join(lock_errors))
    return {
        **identity,
        "activation_utc": activation_utc,
        "output": str(paths.output),
        "registered_opportunities": 0,
        "completed_clusters_by_arm": {arm: 0 for arm in provenance.ARMS},
        "promotion_gate_passed": False,
        "paper_only": True,
        "live_orders": 0,
        "wallet_or_authentication_used": False,
    }


def _position_for_record(record: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    arm = record.get("arm")
    lifecycle_id = record.get("lifecycle_id")
    if not arm or not lifecycle_id:
        return None
    return ((state.get("open_positions") or {}).get(arm) or {}).get(lifecycle_id)


def _bind_state(
    state: dict[str, Any],
    identity: dict[str, str],
    registry_hashes: dict[str, str],
    completion_links: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    bound = provenance.bind_identity({**state, "paper_only": True}, identity)
    for opportunity_id, projection in (bound.get("seen_opportunities") or {}).items():
        if isinstance(projection, dict):
            projection.update(provenance.bind_identity(
                {**projection, "registry_record_hash": registry_hashes.get(opportunity_id)}, identity
            ))
    entered = bound.get("entered_lifecycles") or {}
    for projection in entered.values():
        if isinstance(projection, dict):
            opportunity_id = str(projection.get("opportunity_id") or "")
            projection.update(provenance.bind_identity(
                {**projection, "registry_record_hash": registry_hashes.get(opportunity_id)}, identity
            ))
    for positions in (bound.get("open_positions") or {}).values():
        for projection in positions.values():
            if isinstance(projection, dict):
                opportunity_id = str(projection.get("opportunity_id") or "")
                projection.update(provenance.bind_identity(
                    {**projection, "registry_record_hash": registry_hashes.get(opportunity_id)}, identity
                ))
    for projections in (bound.get("completed_clusters") or {}).values():
        for projection in projections.values():
            if not isinstance(projection, dict):
                continue
            completion_hash = str(projection.get("completion_record_hash") or projection.get("record_hash") or "")
            links = completion_links.get(completion_hash) or {}
            projection.update(provenance.bind_identity(
                {**projection, **links, "completion_record_hash": completion_hash}, identity
            ))
    return bound


def install_runtime_provenance(core: Any, lock: dict[str, Any]) -> None:
    """Bind v5 identity without changing the locked capture/monitor economics."""
    identity = provenance.active_identity(lock)
    registry_rows, _head, registry_errors = provenance.read_and_verify_chain(core.REGISTRY, "registry")
    event_rows, _event_head, event_errors = provenance.read_and_verify_chain(core.EVENTS, "events")
    if registry_errors or event_errors:
        raise SystemExit("v5 chain invalid before runtime provenance installation")
    registry_hashes = {
        str(row.get("opportunity_id")): str(row.get("record_hash"))
        for row in registry_rows if row.get("opportunity_id") and row.get("record_hash")
    }
    event_by_hash = {
        str(row.get("record_hash")): row for row in event_rows if row.get("record_hash")
    }
    completion_links: dict[str, dict[str, Any]] = {}
    original_append = core.append_chain
    original_atomic_json = core.atomic_json

    def append_bound(path: Path, record: dict[str, Any], state: dict[str, Any], chain_name: str) -> dict[str, Any]:
        prepared = dict(record)
        prepared.setdefault("paper_only", True)
        prepared.setdefault("live_order_submitted", False)
        if path == core.REGISTRY:
            sealed = original_append(path, provenance.bind_identity(prepared, identity), state, chain_name)
            opportunity_id = str(sealed.get("opportunity_id") or "")
            if opportunity_id:
                registry_hashes[opportunity_id] = sealed["record_hash"]
            return sealed

        if prepared.get("record_type") == "REBALANCE_ENTRY_EVALUATION" and not prepared.get("opportunity_id"):
            condition_id = str(prepared.get("condition_id") or "")
            if not condition_id:
                raise SystemExit("v5 rebalance entry lacks condition identity")
            opportunity_id = "rebalance_" + condition_id
            prepared["opportunity_id"] = opportunity_id
            if opportunity_id not in registry_hashes:
                lifecycle_id = str(prepared.get("lifecycle_id") or "")
                registered = original_append(
                    core.REGISTRY,
                    provenance.bind_identity({
                        "record_type": "OPPORTUNITY_REGISTERED",
                        "recorded_at": prepared.get("recorded_at"),
                        "decision_time": prepared.get("decision_time"),
                        "opportunity_id": opportunity_id,
                        "provisional_cluster_id": "cluster_" + core.sha_bytes(
                            (condition_id + "|" + lifecycle_id).encode()
                        )[:20],
                        "lifecycle_id": lifecycle_id,
                        "condition_id": condition_id,
                        "yes_token_id": prepared.get("yes_token_id"),
                        "event": prepared.get("event"),
                        "handle": prepared.get("handle"),
                        "bucket": prepared.get("bucket"),
                        "question": prepared.get("question"),
                        "selected_by_frozen_v3": True,
                        "watchdog_filter_version": "v5_rebalance_provenance",
                        "decision_book_timing_quality": None,
                        "decision_book_raw_path": None,
                        "decision_book_sha256": None,
                        "decision_book_request_started_at": None,
                        "decision_book_response_received_at": None,
                        "decision_book_provider_timestamp": None,
                        "pre_activation": False,
                        "historical_replay": False,
                        "paper_only": True,
                        "live_order_submitted": False,
                    }, identity),
                    state,
                    "registry",
                )
                registry_hashes[opportunity_id] = registered["record_hash"]
                state.setdefault("seen_opportunities", {})[opportunity_id] = provenance.bind_identity(
                    {
                        "first_decision_time": prepared.get("decision_time"),
                        "lifecycle_id": lifecycle_id,
                        "condition_id": condition_id,
                        "registry_record_hash": registered["record_hash"],
                    },
                    identity,
                )
                add_ledger_row(registered, lock)

        position = _position_for_record(prepared, state)
        if position:
            prepared.setdefault("opportunity_id", position.get("opportunity_id"))
            prepared.setdefault("entry_record_hash", position.get("entry_record_hash"))
        opportunity_id = str(prepared.get("opportunity_id") or "")
        if opportunity_id:
            prepared.setdefault("registry_record_hash", registry_hashes.get(opportunity_id))
        if prepared.get("record_type") in {"ARM_ENTRY_EVALUATION", "REBALANCE_ENTRY_EVALUATION"}:
            if not prepared.get("opportunity_id") or not prepared.get("registry_record_hash"):
                raise SystemExit("v5 entry lacks exact opportunity registry provenance")
        sealed = original_append(path, provenance.bind_identity(prepared, identity), state, chain_name)
        event_by_hash[sealed["record_hash"]] = sealed
        if sealed.get("record_type") == "ARM_SETTLEMENT" or (
            sealed.get("record_type") == "ARM_EXIT_FILL"
            and float(sealed.get("residual_quantity") or 0) <= 1e-8
        ):
            completion_links[sealed["record_hash"]] = {
                "opportunity_id": sealed.get("opportunity_id"),
                "registry_record_hash": sealed.get("registry_record_hash"),
                "entry_record_hash": sealed.get("entry_record_hash"),
            }
        return sealed

    def atomic_bound(path: Path, value: Any) -> None:
        if isinstance(value, dict) and path == core.STATE:
            value = _bind_state(value, identity, registry_hashes, completion_links)
        elif isinstance(value, dict) and path == core.STATUS:
            value = provenance.bind_identity(value, identity)
        original_atomic_json(path, value)

    def add_ledger_row(record: dict[str, Any], active_lock: dict[str, Any]) -> None:
        core.ensure_ledger()
        row = provenance.bind_identity({
            "opportunity_sequence": record["sequence"],
            "opportunity_id": record["opportunity_id"],
            "provisional_cluster_id": record["provisional_cluster_id"],
            "lifecycle_id": record["lifecycle_id"],
            "condition_id": record.get("condition_id"),
            "yes_token_id": record.get("yes_token_id"),
            "event": record.get("event"),
            "handle": record.get("handle"),
            "bucket": record.get("bucket"),
            "question": record.get("question"),
            "first_discovered_at": record["recorded_at"],
            "decision_time": record["decision_time"],
            "selected_by_frozen_v3": record["selected_by_frozen_v3"],
            "watchdog_filter_version": record["watchdog_filter_version"],
            "decision_book_timing_quality": record.get("decision_book_timing_quality"),
            "decision_book_raw_path": record.get("decision_book_raw_path"),
            "decision_book_sha256": record.get("decision_book_sha256"),
            "decision_book_request_started_at": record.get("decision_book_request_started_at"),
            "decision_book_response_received_at": record.get("decision_book_response_received_at"),
            "decision_book_provider_timestamp": record.get("decision_book_provider_timestamp"),
            "registry_record_hash": record["record_hash"],
        }, identity)
        with core.LEDGER.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=provenance.LEDGER_FIELDS, lineterminator="\n", extrasaction="ignore"
            )
            writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())

    core.LEDGER_FIELDS = provenance.LEDGER_FIELDS
    core.append_chain = append_bound
    core.atomic_json = atomic_bound
    core.add_ledger_row = add_ledger_row


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare a fresh paper-only X v5 experiment")
    subparsers = parser.add_subparsers(dest="action", required=True)
    activate_parser = subparsers.add_parser("activate")
    activate_parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    activate_parser.add_argument("--confirm", required=True)
    activate_parser.add_argument("--deployment-wrapper", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.action != "activate":
        raise ActivationError("unsupported action")
    result = activate(
        args.root,
        confirmation=args.confirm,
        deployment_wrapper=args.deployment_wrapper,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ActivationError as exc:
        print(f"v5 activation refused: {exc}", file=sys.stderr)
        raise SystemExit(2)
