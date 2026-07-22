#!/usr/bin/env python3
"""Experiment-identity binding and verification for X forward evidence."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable


ARMS = ("baseline", "entry_limit_025", "event_risk_cap_10usd", "early_drawdown_exit_25pct")
IDENTITY_FIELDS = ("protocol_id", "protocol_sha256", "baseline_lock_sha256")
ZERO_HASH = "0" * 64
LEDGER_FIELDS = (
    "protocol_id", "protocol_sha256", "baseline_lock_sha256", "experiment_identity_sha256",
    "opportunity_sequence", "opportunity_id", "provisional_cluster_id", "lifecycle_id",
    "condition_id", "yes_token_id", "event", "handle", "bucket", "question",
    "first_discovered_at", "decision_time", "selected_by_frozen_v3", "watchdog_filter_version",
    "decision_book_timing_quality", "decision_book_raw_path", "decision_book_sha256",
    "decision_book_request_started_at", "decision_book_response_received_at",
    "decision_book_provider_timestamp", "registry_record_hash",
)
COMPLETION_TYPES = {"ARM_SETTLEMENT"}
INTERIM_PNL_KEYS = {"aggregate_net_pnl", "mean_expectancy", "arm_ranking"}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha_file(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def active_identity(lock: dict[str, Any]) -> dict[str, str]:
    return {
        "protocol_id": str(lock.get("protocol_id") or ""),
        "protocol_sha256": str(lock.get("protocol_sha256") or ""),
        "baseline_lock_sha256": str(lock.get("lock_sha256") or ""),
    }


def identity_binding_sha256(identity: dict[str, Any]) -> str:
    return sha_bytes(canonical({field: identity.get(field) for field in IDENTITY_FIELDS}))


def bind_identity(record: dict[str, Any], identity: dict[str, str]) -> dict[str, Any]:
    bound = dict(record)
    for field in IDENTITY_FIELDS:
        expected = identity[field]
        existing = bound.get(field)
        if existing not in (None, expected):
            raise ValueError(f"refusing to overwrite foreign experiment identity: {field}")
        bound[field] = expected
    bound["experiment_identity_sha256"] = identity_binding_sha256(identity)
    return bound


def verify_identity_rows(
    rows: Iterable[dict[str, Any]],
    identity: dict[str, str],
    label: str,
    *,
    require_binding: bool = True,
) -> list[str]:
    errors: list[str] = []
    expected_binding = identity_binding_sha256(identity)
    for index, row in enumerate(rows):
        prefix = f"{label}[{index}]"
        for field in IDENTITY_FIELDS:
            if not row.get(field):
                errors.append(f"{prefix}:{field}:missing")
            elif str(row[field]) != identity[field]:
                errors.append(f"{prefix}:{field}:mismatch")
        if require_binding:
            binding = row.get("experiment_identity_sha256")
            if not binding:
                errors.append(f"{prefix}:experiment_identity_sha256:missing")
            elif binding != expected_binding:
                errors.append(f"{prefix}:experiment_identity_sha256:mismatch")
    return errors


def append_chain(path: Path, record: dict[str, Any], chain: dict[str, Any]) -> dict[str, Any]:
    sequence = int(chain.get("sequence", 0)) + 1
    previous_hash = str(chain.get("last_hash") or ZERO_HASH)
    body = {**record, "sequence": sequence, "previous_hash": previous_hash}
    record_hash = sha_bytes(previous_hash.encode() + canonical(body))
    sealed = {**body, "record_hash": record_hash}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(sealed, sort_keys=True, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    chain.update(sequence=sequence, last_hash=record_hash)
    return sealed


def read_and_verify_chain(path: Path, label: str) -> tuple[list[dict[str, Any]], str, list[str]]:
    if not path.is_file():
        return [], ZERO_HASH, [f"{label}:missing"]
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    previous_hash = ZERO_HASH
    for expected_sequence, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"{label}:json:{expected_sequence}")
            continue
        rows.append(row)
        body = {key: value for key, value in row.items() if key != "record_hash"}
        record_hash = sha_bytes(previous_hash.encode() + canonical(body))
        if row.get("sequence") != expected_sequence:
            errors.append(f"{label}:sequence:{expected_sequence}")
        if row.get("previous_hash") != previous_hash:
            errors.append(f"{label}:previous_hash:{expected_sequence}")
        if row.get("record_hash") != record_hash:
            errors.append(f"{label}:record_hash:{expected_sequence}")
        previous_hash = record_hash
    return rows, previous_hash, errors


def _rooted_path(root: Path, raw_path: str) -> Path | None:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return None
    resolved_root = root.resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        return None
    return resolved


def verify_lock(root: Path, lock: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in ("protocol_id", "protocol_sha256", "lock_sha256", "activation_utc"):
        if not lock.get(field):
            errors.append(f"lock:{field}:missing")
    body = {key: value for key, value in lock.items() if key != "lock_sha256"}
    if lock.get("lock_sha256") != sha_bytes(canonical(body)):
        errors.append("lock:lock_sha256:mismatch")
    protocol_path = _rooted_path(root, str(lock.get("protocol_path") or ""))
    if protocol_path is None or not protocol_path.is_file():
        errors.append("lock:protocol_path:missing_or_unsafe")
    elif sha_file(protocol_path) != lock.get("protocol_sha256"):
        errors.append("lock:protocol_sha256:mismatch")
    sources = lock.get("locked_source_sha256")
    if not isinstance(sources, dict) or not sources:
        errors.append("lock:locked_source_sha256:missing")
    else:
        for raw_path, expected in sorted(sources.items()):
            source = _rooted_path(root, str(raw_path))
            if source is None or not source.is_file():
                errors.append(f"lock:locked_source_sha256:missing_or_unsafe:{raw_path}")
            elif sha_file(source) != expected:
                errors.append(f"lock:locked_source_sha256:mismatch:{raw_path}")
    external_sources = lock.get("external_locked_source_sha256")
    if not isinstance(external_sources, dict) or not external_sources:
        errors.append("lock:external_locked_source_sha256:missing")
    else:
        for raw_path, expected in sorted(external_sources.items()):
            source = Path(raw_path)
            if not source.is_absolute() or not source.is_file():
                errors.append(f"lock:external_locked_source_sha256:missing_or_unsafe:{raw_path}")
            elif sha_file(source) != expected:
                errors.append(f"lock:external_locked_source_sha256:mismatch:{raw_path}")
    if lock.get("paper_only") is not True:
        errors.append("lock:paper_only:not_true")
    if lock.get("live_orders_allowed") is not False:
        errors.append("lock:live_orders_allowed:not_false")
    if lock.get("wallet_or_authentication_allowed") is not False:
        errors.append("lock:wallet_or_authentication_allowed:not_false")
    return errors


def _completion_record(row: dict[str, Any]) -> bool:
    if row.get("record_type") in COMPLETION_TYPES:
        return True
    if row.get("record_type") == "ARM_EXIT_FILL":
        try:
            return float(row.get("residual_quantity")) <= 1e-8
        except (TypeError, ValueError):
            return False
    return row.get("completed_cluster") is True


def _index_unique(
    rows: Iterable[dict[str, Any]], field: str, label: str, errors: list[str]
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        value = str(row.get(field) or "")
        if value:
            grouped.setdefault(value, []).append(row)
    for value, matches in grouped.items():
        if len(matches) != 1:
            errors.append(f"{label}:{field}:ambiguous:{value}")
    return {value: matches[0] for value, matches in grouped.items() if len(matches) == 1}


def verify_experiment_provenance(
    lock: dict[str, Any],
    registry: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    events: list[dict[str, Any]],
    state: dict[str, Any],
    status: dict[str, Any],
    *,
    require_binding: bool = True,
) -> list[str]:
    identity = active_identity(lock)
    errors: list[str] = []
    for field, value in identity.items():
        if not value:
            errors.append(f"active_identity:{field}:missing")
    errors.extend(verify_identity_rows(registry, identity, "registry", require_binding=require_binding))
    errors.extend(verify_identity_rows(ledger, identity, "ledger", require_binding=require_binding))
    errors.extend(verify_identity_rows(events, identity, "events", require_binding=require_binding))
    errors.extend(verify_identity_rows([state], identity, "state", require_binding=require_binding))
    errors.extend(verify_identity_rows([status], identity, "status", require_binding=require_binding))

    registry_by_hash = _index_unique(registry, "record_hash", "registry", errors)
    registry_by_opportunity = _index_unique(registry, "opportunity_id", "registry", errors)
    events_by_hash = _index_unique(events, "record_hash", "events", errors)
    for index, row in enumerate(registry):
        if row.get("paper_only") is not True or row.get("live_order_submitted") is not False:
            errors.append(f"registry[{index}]:paper_safety:invalid")
    for index, row in enumerate(events):
        if row.get("paper_only") is not True or row.get("live_order_submitted") is not False:
            errors.append(f"events[{index}]:paper_safety:invalid")

    if len(ledger) != len(registry):
        errors.append("ledger:registry_count:mismatch")
    for index, row in enumerate(ledger):
        prefix = f"ledger[{index}]"
        registry_hash = str(row.get("registry_record_hash") or "")
        linked = registry_by_hash.get(registry_hash)
        if not registry_hash:
            errors.append(f"{prefix}:registry_record_hash:missing")
        elif linked is None:
            errors.append(f"{prefix}:registry_record_hash:unlinked")
        elif row.get("opportunity_id") != linked.get("opportunity_id"):
            errors.append(f"{prefix}:opportunity_id:registry_mismatch")
        if linked is not None and str(row.get("opportunity_sequence") or "") != str(linked.get("sequence")):
            errors.append(f"{prefix}:opportunity_sequence:registry_mismatch")

    eligible_entries = [
        row for row in events
        if row.get("record_type") in {"ARM_ENTRY_EVALUATION", "REBALANCE_ENTRY_EVALUATION"}
        and row.get("execution_evidence_eligible") is True
    ]
    for row in eligible_entries:
        record_hash = str(row.get("record_hash") or "missing")
        registry_hash = str(row.get("registry_record_hash") or "")
        opportunity_id = str(row.get("opportunity_id") or "")
        if not registry_hash:
            errors.append(f"entry:{record_hash}:registry_record_hash:missing")
        elif registry_hash not in registry_by_hash:
            errors.append(f"entry:{record_hash}:registry_record_hash:unlinked")
        if not opportunity_id:
            errors.append(f"entry:{record_hash}:opportunity_id:missing")
        elif opportunity_id not in registry_by_opportunity:
            errors.append(f"entry:{record_hash}:opportunity_id:unlinked")
        elif registry_hash and registry_by_opportunity[opportunity_id].get("record_hash") != registry_hash:
            errors.append(f"entry:{record_hash}:opportunity_registry_link:mismatch")
        if row.get("decision") not in {"PAPER_ENTRY", "PAPER_REBALANCE_ENTRY"}:
            errors.append(f"entry:{record_hash}:decision:invalid")
        if row.get("paper_only") is not True or row.get("live_order_submitted") is not False:
            errors.append(f"entry:{record_hash}:paper_safety:invalid")

    completion_events = [row for row in events if _completion_record(row)]
    for row in completion_events:
        completion_hash = str(row.get("record_hash") or "missing")
        entry_hash = str(row.get("entry_record_hash") or "")
        registry_hash = str(row.get("registry_record_hash") or "")
        if not entry_hash:
            errors.append(f"completion:{completion_hash}:entry_record_hash:missing")
            continue
        entry = events_by_hash.get(entry_hash)
        if entry is None:
            errors.append(f"completion:{completion_hash}:entry_record_hash:unlinked")
            continue
        if entry not in eligible_entries:
            errors.append(f"completion:{completion_hash}:entry_record_hash:not_eligible_entry")
        for field in ("opportunity_id", "registry_record_hash", "arm", "lifecycle_id"):
            if row.get(field) != entry.get(field):
                errors.append(f"completion:{completion_hash}:{field}:entry_mismatch")
        if not registry_hash or registry_hash not in registry_by_hash:
            errors.append(f"completion:{completion_hash}:registry_record_hash:unlinked")
        if row.get("paper_only") is not True or row.get("live_order_submitted") is not False:
            errors.append(f"completion:{completion_hash}:paper_safety:invalid")

    seen = state.get("seen_opportunities") or {}
    if not isinstance(seen, dict):
        errors.append("state:seen_opportunities:invalid")
        seen = {}
    for opportunity_id, projection in seen.items():
        if not isinstance(projection, dict):
            errors.append(f"state:seen_opportunities:{opportunity_id}:invalid")
            continue
        errors.extend(verify_identity_rows(
            [projection], identity, f"state.seen[{opportunity_id}]", require_binding=require_binding
        ))
        expected = registry_by_opportunity.get(str(opportunity_id))
        if expected is None or projection.get("registry_record_hash") != expected.get("record_hash"):
            errors.append(f"state:seen_opportunities:{opportunity_id}:registry_link:mismatch")

    entered = state.get("entered_lifecycles") or {}
    if not isinstance(entered, dict):
        errors.append("state:entered_lifecycles:invalid")
        entered = {}
    for lifecycle_id, projection in entered.items():
        prefix = f"state.entered[{lifecycle_id}]"
        if not isinstance(projection, dict):
            errors.append(f"{prefix}:invalid")
            continue
        errors.extend(verify_identity_rows(
            [projection], identity, prefix, require_binding=require_binding
        ))
        opportunity_id = str(projection.get("opportunity_id") or "")
        registry_row = registry_by_opportunity.get(opportunity_id)
        if registry_row is None or projection.get("registry_record_hash") != registry_row.get("record_hash"):
            errors.append(f"{prefix}:registry_link:mismatch")

    open_positions = state.get("open_positions") or {}
    for arm in ARMS:
        positions = open_positions.get(arm) or {}
        if not isinstance(positions, dict):
            errors.append(f"state:open_positions:{arm}:invalid")
            continue
        for lifecycle_id, projection in positions.items():
            prefix = f"state.open[{arm}][{lifecycle_id}]"
            if not isinstance(projection, dict):
                errors.append(f"{prefix}:invalid")
                continue
            errors.extend(verify_identity_rows(
                [projection], identity, prefix, require_binding=require_binding
            ))
            entry = events_by_hash.get(str(projection.get("entry_record_hash") or ""))
            if entry is None or entry not in eligible_entries:
                errors.append(f"{prefix}:entry_record_hash:unlinked")
                continue
            for field in ("opportunity_id", "registry_record_hash"):
                if projection.get(field) != entry.get(field):
                    errors.append(f"{prefix}:{field}:entry_mismatch")

    completed = state.get("completed_clusters") or {}
    completed_counts = {arm: 0 for arm in ARMS}
    for arm in ARMS:
        projections = completed.get(arm) or {}
        if not isinstance(projections, dict):
            errors.append(f"state:completed_clusters:{arm}:invalid")
            continue
        completed_counts[arm] = len(projections)
        for lifecycle_id, projection in projections.items():
            prefix = f"state.completed[{arm}][{lifecycle_id}]"
            if not isinstance(projection, dict):
                errors.append(f"{prefix}:invalid")
                continue
            errors.extend(verify_identity_rows(
                [projection], identity, prefix, require_binding=require_binding
            ))
            completion_hash = str(
                projection.get("completion_record_hash") or projection.get("record_hash") or ""
            )
            completion = events_by_hash.get(completion_hash)
            if completion is None or not _completion_record(completion):
                errors.append(f"{prefix}:completion_record_hash:unlinked")
                continue
            for field in ("opportunity_id", "registry_record_hash", "entry_record_hash"):
                if projection.get(field) != completion.get(field):
                    errors.append(f"{prefix}:{field}:completion_mismatch")
            if completion.get("arm") != arm or completion.get("lifecycle_id") != lifecycle_id:
                errors.append(f"{prefix}:arm_or_lifecycle:mismatch")

    if status.get("paper_only") is not True:
        errors.append("status:paper_only:not_true")
    if status.get("live_orders") != 0:
        errors.append("status:live_orders:not_zero")
    if status.get("wallet_or_authentication_used") is not False:
        errors.append("status:wallet_or_authentication_used:not_false")
    if status.get("promotion_gate_passed") is not False:
        errors.append("status:promotion_gate_passed:not_false")
    if status.get("aggregate_pnl_hidden_until_fixed_end") is not True:
        errors.append("status:aggregate_pnl_hidden_until_fixed_end:not_true")
    for key in INTERIM_PNL_KEYS:
        if key in status:
            errors.append(f"status:{key}:interim_pnl_exposed")
    if status.get("registered_opportunities") != len(registry):
        errors.append("status:registered_opportunities:mismatch")
    for field in (
        "executable_completed_clusters_by_arm",
        "net_capturable_completed_clusters_by_arm",
    ):
        reported = status.get(field) or {}
        for arm, expected_count in completed_counts.items():
            if reported.get(arm) != expected_count:
                errors.append(f"status:{field}:{arm}:mismatch")

    eligible_lifecycles_by_arm = {
        arm: {
            str(row.get("lifecycle_id")) for row in eligible_entries
            if row.get("arm") == arm and row.get("lifecycle_id")
        }
        for arm in ARMS
    }
    for arm in ARMS:
        open_count = len(open_positions.get(arm) or {})
        if len(eligible_lifecycles_by_arm[arm]) != open_count + completed_counts[arm]:
            errors.append(f"state:entry_position_completion_reconciliation:{arm}:mismatch")
    return errors
