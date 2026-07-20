#!/usr/bin/env python3
"""Normalize scanner signals into an append-only event evidence ledger.

The ledger is intentionally immutable: an existing event_id is never rewritten.
Any future correction must be represented as a separate revision/event, preserving
what the pipeline knew at the original decision time.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "reports" / "public_record_reaction_signals.jsonl"
DEFAULT_LEDGER = ROOT / "reports" / "event_evidence_ledger_v2.jsonl"
PIPELINE_VERSION = "event_evidence_v2_2026_07_17"
INTRADAY_WINDOWS = ["1m", "5m", "15m", "60m", "next_open", "1d", "5d"]
CONSERVATIVE_WINDOWS = ["1d", "5d"]


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_prefixed(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8", "replace")).hexdigest()


def parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace(" ", "T", 1) if " " in text and "T" not in text else text
    if len(normalized) == 10:
        normalized += "T00:00:00Z"
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def milliseconds_between(start: Any, end: Any) -> int | None:
    start_dt = parse_utc(start)
    end_dt = parse_utc(end)
    if start_dt is None or end_dt is None:
        return None
    return round((end_dt - start_dt).total_seconds() * 1000)


def infer_source_timestamp(signal: dict[str, Any]) -> tuple[str | None, str, str]:
    explicit = signal.get("source_published_at")
    explicit_precision = signal.get("source_timestamp_precision")
    if explicit:
        return str(explicit), str(explicit_precision or "unknown"), "Timestamp supplied by source adapter."

    raw = signal.get("raw") if isinstance(signal.get("raw"), dict) else {}
    source = str(signal.get("source") or "")
    if source == "SEC_EDGAR" and raw.get("updated"):
        return str(raw["updated"]), "exact_second", "Recovered from SEC Atom entry updated timestamp."

    record_date = signal.get("record_date")
    if not record_date:
        return None, "unknown", "Legacy signal has no source publication timestamp."
    text = str(record_date)
    if source == "USAspending":
        if len(text) > 10:
            return text, "source_clock_unverified", (
                "Legacy USAspending last-modified clock retained, but timezone/publication semantics are unverified; "
                "intraday reaction windows are disabled."
            )
        return text, "date_only", "Legacy USAspending source exposes only day-level timing for this event."
    if len(text) == 10:
        return text, "date_only", "Legacy source record contains only a calendar date."
    return text, "source_clock_unverified", "Legacy timestamp has no verified timezone/source-publication semantics."


def normalize_polymarket_targets(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    targets: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        target = {
            "condition_id": item.get("condition_id") or item.get("conditionId"),
            "market_id": item.get("market_id") or item.get("id"),
            "slug": item.get("slug"),
            "question": item.get("question") or item.get("headline"),
            "token_id": item.get("token_id"),
            "outcome": item.get("outcome"),
            "token_ids": item.get("token_ids") or item.get("clobTokenIds"),
            "outcomes": item.get("outcomes"),
            "paper_side": item.get("paper_side"),
            "paper_outcome": item.get("paper_outcome"),
            "paper_quantity": item.get("paper_quantity"),
            "observe_exact_token": bool(item.get("observe_exact_token")),
            "match_score": item.get("score") or item.get("match_score"),
        }
        if any(v not in (None, "") for v in target.values()):
            targets.append(target)
    return targets


def normalize_signal(signal: dict[str, Any], *, ingested_at: str | None = None) -> dict[str, Any]:
    ingested_at = ingested_at or utc_now()
    source = str(signal.get("source") or "unknown")
    source_id = str(signal.get("id") or sha256_prefixed(signal))
    logical_digest = hashlib.sha256(f"{source}|{source_id}".encode("utf-8", "replace")).hexdigest()[:24]
    logical_event_id = f"evt_{logical_digest}"
    revision_id = signal.get("revision_id") or signal.get("adapter_revision")
    revision_material = f"{logical_event_id}|{PIPELINE_VERSION}"
    if revision_id:
        revision_basis = {
            "source_published_at": signal.get("source_published_at") or signal.get("feed_published_at") or signal.get("record_date"),
            "source_timestamp_precision": signal.get("source_timestamp_precision"),
            "record_type": signal.get("record_type"),
            "headline": signal.get("headline"),
            "ticker": signal.get("ticker"),
            "company": signal.get("company"),
            "polymarket_matches": signal.get("polymarket_matches"),
            "raw": signal.get("raw") if isinstance(signal.get("raw"), dict) else {},
        }
        revision_material += f"|{revision_id}|{sha256_prefixed(revision_basis)}"
    revision_digest = hashlib.sha256(
        revision_material.encode("utf-8", "replace")
    ).hexdigest()[:24]
    event_id = f"evr_{revision_digest}"

    source_published_at, precision, timestamp_notes = infer_source_timestamp(signal)
    first_seen_at = signal.get("first_seen_at") or signal.get("response_received_at") or signal.get("detected_at")
    request_started_at = signal.get("request_started_at")
    response_received_at = signal.get("response_received_at") or signal.get("fetched_at") or first_seen_at
    fetched_at = signal.get("fetched_at") or response_received_at
    parsed_at = signal.get("parsed_at") or signal.get("detected_at") or fetched_at
    decision_at = signal.get("decision_at") or signal.get("detected_at") or parsed_at
    legacy_approximation = not any(
        key in signal for key in ("source_published_at", "fetched_at", "parsed_at", "decision_at")
    )
    if legacy_approximation:
        timestamp_notes += " Legacy stage timestamps use detected_at as an approximation."

    stock_target = None
    ticker = signal.get("ticker")
    if ticker:
        stock_target = {
            "ticker": str(ticker).upper(),
            "company": signal.get("company"),
            "cik": signal.get("cik"),
            "mapping_method": signal.get("match_method"),
            "mapping_confidence": signal.get("match_confidence"),
        }

    default_windows = INTRADAY_WINDOWS if precision in {"exact_second", "exact_millisecond", "minute"} else CONSERVATIVE_WINDOWS
    explicit_windows = signal.get("eligible_markout_windows")
    eligible = list(explicit_windows) if isinstance(explicit_windows, list) else list(default_windows)
    raw_record = signal.get("raw") if isinstance(signal.get("raw"), dict) else {}
    event = {
        "schema_version": 2,
        "measurement_version": PIPELINE_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "event_id": event_id,
        "logical_event_id": logical_event_id,
        "revision_id": revision_id,
        "supersedes_event_id": signal.get("supersedes_event_id"),
        "source_event_id": source_id,
        "source": source,
        "source_group": signal.get("source_group"),
        "event_type": signal.get("record_type"),
        "headline": signal.get("headline"),
        "company": signal.get("company"),
        "source_url": signal.get("url"),
        "source_published_at": source_published_at,
        "source_timestamp_precision": precision,
        "timestamp_notes": timestamp_notes,
        "first_seen_at": first_seen_at,
        "request_started_at": request_started_at,
        "response_received_at": response_received_at,
        "fetched_at": fetched_at,
        "parsed_at": parsed_at,
        "decision_at": decision_at,
        "ledger_ingested_at": ingested_at,
        "latency_ms": {
            "source_to_first_seen": milliseconds_between(source_published_at, first_seen_at)
            if precision in {"exact_second", "exact_millisecond", "minute"}
            else None,
            "request_to_response": milliseconds_between(request_started_at, response_received_at),
            "response_to_parse": milliseconds_between(response_received_at, parsed_at),
            "fetch_to_parse": milliseconds_between(fetched_at, parsed_at),
            "parse_to_decision": milliseconds_between(parsed_at, decision_at),
            "first_seen_to_decision": milliseconds_between(first_seen_at, decision_at),
        },
        "raw_payload_hash": sha256_prefixed(raw_record),
        "parser_version": PIPELINE_VERSION,
        "strategy_version": signal.get("version"),
        "strategy_name": signal.get("strategy_name"),
        "strategy_evidence_version": signal.get("strategy_evidence_version"),
        "decision_action": signal.get("decision_action"),
        "decision_mode": signal.get("decision_mode"),
        "market_lifecycle_id": signal.get("market_lifecycle_id"),
        "strategy_position_id": signal.get("strategy_position_id"),
        "primary_horizon": signal.get("primary_horizon"),
        "market_state_eligible": signal.get("market_state_eligible", True),
        "market_state_evidence_limitation": signal.get("market_state_evidence_limitation"),
        "signal_score": signal.get("signal_score"),
        "priority": signal.get("priority"),
        "reason": signal.get("reason"),
        "eligible_markout_windows": eligible,
        "targets": {
            "stock": stock_target,
            "polymarket": normalize_polymarket_targets(signal.get("polymarket_matches")),
        },
        "provenance": {
            "legacy_stage_timestamp_approximation": legacy_approximation,
            "record_date": signal.get("record_date"),
            "record_date_type": signal.get("record_date_type"),
            "raw_record": raw_record,
        },
    }
    return event


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
            if isinstance(row, dict):
                rows.append(row)
    return rows


def active_events(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return append-only records that have not been explicitly superseded."""
    materialized = list(rows)
    superseded = {str(row.get("supersedes_event_id")) for row in materialized if row.get("supersedes_event_id")}
    return [row for row in materialized if str(row.get("event_id")) not in superseded]


def ingest(input_path: Path, ledger_path: Path) -> dict[str, Any]:
    ledger_rows = list(read_jsonl(ledger_path))
    existing_ids = {str(row.get("event_id")) for row in ledger_rows if row.get("event_id")}
    latest_by_logical = {
        str(row.get("logical_event_id")): row for row in active_events(ledger_rows) if row.get("logical_event_id")
    }
    scanned = 0
    duplicates = 0
    appended_rows: list[dict[str, Any]] = []
    ingested_at = utc_now()
    for signal in read_jsonl(input_path):
        scanned += 1
        event = normalize_signal(signal, ingested_at=ingested_at)
        if event["event_id"] in existing_ids:
            duplicates += 1
            continue
        prior = latest_by_logical.get(str(event.get("logical_event_id")))
        if event.get("revision_id") and not event.get("supersedes_event_id") and prior:
            event["supersedes_event_id"] = prior.get("event_id")
        existing_ids.add(event["event_id"])
        appended_rows.append(event)
        latest_by_logical[str(event.get("logical_event_id"))] = event

    if appended_rows:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with ledger_path.open("a", encoding="utf-8") as handle:
            for event in appended_rows:
                handle.write(canonical_json(event) + "\n")
    return {
        "input": str(input_path),
        "ledger": str(ledger_path),
        "scanned": scanned,
        "appended": len(appended_rows),
        "duplicates": duplicates,
        "ledger_events": len(existing_ids),
        "pipeline_version": PIPELINE_VERSION,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build append-only public-record event evidence ledger")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        result = ingest(args.input, args.ledger)
    except Exception as exc:
        print(f"event ledger error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
