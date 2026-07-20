#!/usr/bin/env python3
"""Shared, append-only strategy-decision evidence adapters.

This module does not place orders. It converts paper strategy decisions into the
same immutable signal format consumed by event_ledger.py. Historical replay rows
remain explicitly unverified; a stored top-of-book is not promoted into causal
execution evidence unless exact request/response timing is present.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import event_ledger

STRATEGY_EVIDENCE_VERSION = "strategy_decision_evidence_v1_2026_07_18"


def _decision_id(material: dict[str, Any]) -> str:
    digest = hashlib.sha256(event_ledger.canonical_json(material).encode("utf-8", "replace")).hexdigest()[:28]
    return f"strategy_decision_{digest}"


def normalize_levels(value: Any) -> list[dict[str, str]]:
    """Normalize dict or [price,size] book levels without inventing depth."""
    if not isinstance(value, (list, tuple)):
        return []
    levels: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            price, size = item.get("price"), item.get("size")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            price, size = item[0], item[1]
        else:
            continue
        if price is None or size is None:
            continue
        levels.append({"price": str(price), "size": str(size)})
    return levels


def build_book(
    *,
    bids: Any = None,
    asks: Any = None,
    best_bid: Any = None,
    best_ask: Any = None,
    request_started_at: str | None = None,
    response_received_at: str | None = None,
    provider_timestamp: str | None = None,
    timing_quality: str | None = None,
    capture_source: str | None = None,
) -> dict[str, Any] | None:
    normalized_bids = normalize_levels(bids)
    normalized_asks = normalize_levels(asks)
    if not normalized_bids and best_bid is not None:
        normalized_bids = normalize_levels([best_bid])
    if not normalized_asks and best_ask is not None:
        normalized_asks = normalize_levels([best_ask])
    if not normalized_bids and not normalized_asks:
        return None
    if timing_quality is None:
        timing_quality = (
            "exact_request_response"
            if request_started_at and response_received_at
            else "snapshot_timestamp_only"
        )
    return {
        "bids": normalized_bids,
        "asks": normalized_asks,
        "request_started_at": request_started_at,
        "response_received_at": response_received_at,
        "provider_timestamp": provider_timestamp,
        "timing_quality": timing_quality,
        "capture_source": capture_source or "strategy_decision_input",
    }


def build_decision_signal(
    *,
    strategy: str,
    strategy_version: str | None,
    action: str,
    lifecycle_id: str,
    position_id: str | int | None,
    decision_at: str,
    decision_mode: str,
    condition_id: str | None,
    token_id: str | None,
    outcome: str | None,
    quantity: float | None,
    question: str | None,
    book: dict[str, Any] | None = None,
    market_id: str | None = None,
    slug: str | None = None,
    eligible_markout_windows: list[str] | None = None,
    primary_horizon: str | None = None,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_action = str(action).upper()
    material = {
        "strategy": strategy,
        "strategy_version": strategy_version,
        "action": normalized_action,
        "lifecycle_id": str(lifecycle_id),
        "position_id": None if position_id is None else str(position_id),
        "decision_at": decision_at,
        "condition_id": condition_id,
        "token_id": token_id,
        "outcome": outcome,
    }
    source_id = _decision_id(material)
    book = dict(book) if isinstance(book, dict) else None
    request_started_at = book.get("request_started_at") if book else None
    response_received_at = book.get("response_received_at") if book else None
    timing_quality = book.get("timing_quality") if book else None
    raw = {
        "strategy": strategy,
        "strategy_version": strategy_version,
        "decision_action": normalized_action,
        "market_lifecycle_id": str(lifecycle_id),
        "strategy_position_id": position_id,
        "decision_mode": decision_mode,
        "primary_horizon": primary_horizon,
        "order_book": book,
        "metadata": metadata or {},
    }
    target = {
        "condition_id": condition_id,
        "market_id": market_id,
        "slug": slug,
        "question": question,
        "token_id": token_id,
        "outcome": outcome,
        "paper_quantity": quantity,
        "observe_exact_token": bool(token_id),
    }
    if normalized_action == "ENTRY":
        target["paper_side"] = "buy"
        target["paper_outcome"] = outcome
    signal = {
        "id": source_id,
        "source": "STRATEGY_DECISION",
        "source_group": f"strategy:{strategy}",
        "record_type": f"strategy_{normalized_action.lower()}_decision",
        "headline": question or f"{strategy} {normalized_action}",
        "source_published_at": decision_at,
        "source_timestamp_precision": "exact_millisecond" if "." in decision_at else "exact_second",
        "first_seen_at": response_received_at or decision_at,
        "request_started_at": request_started_at,
        "response_received_at": response_received_at or decision_at,
        "fetched_at": response_received_at or decision_at,
        "parsed_at": decision_at,
        "decision_at": decision_at,
        "version": strategy_version,
        "reason": reason,
        "polymarket_matches": [target] if any((condition_id, market_id, slug, token_id)) else [],
        "eligible_markout_windows": list(eligible_markout_windows or []),
        "strategy_name": strategy,
        "decision_action": normalized_action,
        "market_lifecycle_id": str(lifecycle_id),
        "strategy_position_id": position_id,
        "decision_mode": decision_mode,
        "primary_horizon": primary_horizon,
        "strategy_evidence_version": STRATEGY_EVIDENCE_VERSION,
        "market_state_eligible": bool(book) and normalized_action in {"ENTRY", "EXIT", "SWITCH_EXIT", "SWITCH_ENTRY"},
        "raw": raw,
    }
    if timing_quality != "exact_request_response":
        signal["market_state_evidence_limitation"] = "Stored strategy row lacks exact CLOB request/response timing."
    return signal


def append_signals(path: Path, signals: Iterable[dict[str, Any]]) -> dict[str, Any]:
    path = Path(path)
    existing = {str(row.get("id")) for row in event_ledger.read_jsonl(path) if row.get("id")}
    pending: list[dict[str, Any]] = []
    seen = set(existing)
    for signal in signals:
        identifier = str(signal.get("id") or "")
        if not identifier or identifier in seen:
            continue
        pending.append(signal)
        seen.add(identifier)
    if pending:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for signal in pending:
                handle.write(event_ledger.canonical_json(signal) + "\n")
    return {"path": str(path), "appended": len(pending), "duplicates": len(existing), "total": len(seen)}
