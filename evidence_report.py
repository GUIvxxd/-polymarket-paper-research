#!/usr/bin/env python3
"""Build conservative v2 event-evidence reports without profitability claims."""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import event_ledger
import markout_worker

ROOT = Path(__file__).resolve().parent
MEASUREMENT_VERSION = "event_evidence_v2_2026_07_17"
DEFAULT_EVENTS = ROOT / "reports" / "event_evidence_ledger_v2.jsonl"
DEFAULT_STATES = ROOT / "reports" / "event_market_states_v2.jsonl"
DEFAULT_MARKOUTS = ROOT / "reports" / "event_markouts_v2.jsonl"
DEFAULT_JSON = ROOT / "reports" / "event_evidence_report_latest.json"
DEFAULT_MD = ROOT / "reports" / "event_evidence_report_latest.md"
DEFAULT_CSV = ROOT / "reports" / "event_evidence_aggregates_latest.csv"
MIN_SAMPLE = 30


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def as_number(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def distribution(values: Iterable[float]) -> dict[str, Any]:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    if not clean:
        return {"n": 0, "mean": None, "median": None, "min": None, "max": None, "positive_rate": None}
    return {
        "n": len(clean), "mean": statistics.fmean(clean), "median": statistics.median(clean),
        "min": min(clean), "max": max(clean),
        "positive_rate": sum(value > 0 for value in clean) / len(clean),
    }


def count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    return dict(sorted(Counter(str(row.get(key) or "unknown") for row in rows).items()))


def _observation_signature(row: dict[str, Any], snapshot: dict[str, Any]) -> tuple[Any, ...]:
    baseline = row.get("baseline_state") if isinstance(row.get("baseline_state"), dict) else {}
    later = row.get("later_state") if isinstance(row.get("later_state"), dict) else {}
    target = snapshot.get("ticker") or snapshot.get("token_id") or baseline.get("ticker") or baseline.get("token_id")
    baseline_timestamp = baseline.get("market_timestamp")
    later_timestamp = later.get("market_timestamp")
    if not baseline_timestamp or not later_timestamp:
        return ("row", row.get("markout_id") or row.get("snapshot_id") or id(row))
    return (
        row.get("asset_type"), target, row.get("window"),
        baseline_timestamp, later_timestamp,
        baseline.get("token_id"), later.get("token_id"),
    )


def aggregate_markouts(
    markouts: list[dict[str, Any]], events: dict[str, dict[str, Any]], snapshots: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in markouts:
        event = events.get(str(row.get("event_id")), {})
        snapshot = snapshots.get(str(row.get("snapshot_id")), {})
        target = snapshot.get("ticker") or snapshot.get("token_id") or "unknown"
        key = (
            str(event.get("source") or row.get("source") or "unknown"),
            str(event.get("event_type") or row.get("event_type") or "unknown"),
            str(event.get("source_timestamp_precision") or row.get("source_timestamp_precision") or "unknown"),
            str(row.get("asset_type") or snapshot.get("asset_type") or "unknown"),
            str(target), str(row.get("window") or "unknown"),
        )
        groups[key].append(row)

    output: list[dict[str, Any]] = []
    for key, rows in sorted(groups.items()):
        event_valid = [row for row in rows if row.get("valid_timing") is True]
        independent: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in event_valid:
            snapshot = snapshots.get(str(row.get("snapshot_id")), {})
            independent.setdefault(_observation_signature(row, snapshot), row)
        market_values = [value for row in independent.values()
                         if (value := as_number(row.get("market_response_return"))) is not None]
        gross_values = [value for row in independent.values()
                        if row.get("gross_top_of_book_feasible") is True
                        and (value := as_number(row.get("gross_top_of_book_return"))) is not None]
        source, event_type, precision, asset_type, target, window = key
        output.append({
            "source": source, "event_type": event_type, "timestamp_precision": precision,
            "asset_type": asset_type, "target": target, "window": window,
            "event_rows": len(rows), "valid_event_rows": len(event_valid),
            "independent_observations": len(independent),
            "market_response": distribution(market_values),
            "gross_top_of_book": distribution(gross_values),
            "minimum_sample_met": len(gross_values) >= MIN_SAMPLE,
            "edge_claim_allowed": False,
        })
    return output


def expected_windows(snapshot: dict[str, Any]) -> set[str]:
    if snapshot.get("valid_baseline") is not True or markout_worker.reference_price(snapshot) is None:
        return set()
    return markout_worker.allowed_windows(snapshot)


def build_report(
    events: list[dict[str, Any]], snapshots: list[dict[str, Any]], markouts: list[dict[str, Any]], *, generated_at: str | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or utc_now()
    events = event_ledger.active_events(events)
    active_event_ids = {str(row.get("event_id")) for row in events}
    snapshots = [row for row in snapshots if str(row.get("event_id")) in active_event_ids]
    active_snapshot_ids = {str(row.get("snapshot_id")) for row in snapshots}
    markouts = [row for row in markouts
                if str(row.get("event_id")) in active_event_ids
                and str(row.get("snapshot_id")) in active_snapshot_ids]
    event_index = {str(row.get("event_id")): row for row in events}
    snapshot_index = {str(row.get("snapshot_id")): row for row in snapshots}
    aggregates = aggregate_markouts(markouts, event_index, snapshot_index)
    target_events = [event for event in events if (event.get("targets") or {}).get("stock")
                     or (event.get("targets") or {}).get("polymarket")]
    snap_events = {str(row.get("event_id")) for row in snapshots}
    valid_rows = [row for row in markouts if row.get("valid_timing") is True]
    valid_response = [row for row in valid_rows if as_number(row.get("market_response_return")) is not None]
    gross_feasible = [row for row in valid_rows if row.get("gross_top_of_book_feasible") is True
                      and as_number(row.get("gross_top_of_book_return")) is not None]
    independent_signatures = {
        _observation_signature(row, snapshot_index.get(str(row.get("snapshot_id")), {})) for row in valid_response
    }
    strict_execution_states = [
        row for row in snapshots
        if row.get("execution_evidence_eligible") is True
        or (
            "execution_evidence_eligible" not in row
            and row.get("executable_quote") is True
            and row.get("valid_baseline") is True
            and (row.get("asset_type") != "polymarket" or row.get("mapping_verified") is True)
        )
    ]
    strategy_events = [event for event in events if event.get("strategy_name")]
    strategy_event_ids = {str(event.get("event_id")) for event in strategy_events}
    strategy_states = [row for row in snapshots if str(row.get("event_id")) in strategy_event_ids]
    expected = sum(len(expected_windows(row)) for row in snapshots if row.get("valid_baseline") is True)
    if gross_feasible:
        conclusion = "gross_top_of_book_feasibility_observed_net_capture_unproven"
    elif valid_response:
        conclusion = "market_response_observed_but_capturable_edge_unproven"
    else:
        conclusion = "measurement_accumulating_no_valid_edge_evidence"
    blockers = []
    if not snapshots:
        blockers.append("No v2 causal market baselines have been recorded.")
    if not any(row.get("executable_quote") is True for row in snapshots):
        blockers.append("No v2 baseline has executable bid/ask with positive displayed size.")
    if not gross_feasible:
        blockers.append("No timing-valid, predeclared, size-feasible gross top-of-book round trip exists.")
    blockers.append("Net capturability remains unproven until fees, slippage, queue position, fill probability, and settlement costs are modeled.")
    return {
        "schema_version": 2, "measurement_version": MEASUREMENT_VERSION,
        "generated_at": generated_at, "paper_only": True, "live_orders": False,
        "conclusion": conclusion, "profitability_claim": False, "edge_claim_allowed": False,
        "invalidated_prior_measurement_versions": ["event_evidence_v1_2026_07_17"],
        "counts": {
            "events": len(events), "target_bearing_events": len(target_events), "snapshots": len(snapshots),
            "valid_baselines": sum(row.get("valid_baseline") is True for row in snapshots),
            "events_with_any_snapshot": len(snap_events),
            "events_without_snapshot": sum(str(event.get("event_id")) not in snap_events for event in target_events),
            "executable_at_capture_snapshots": sum(row.get("executable_quote") is True for row in snapshots),
            "markouts": len(markouts), "valid_market_response_markouts": len(valid_response),
            "independent_valid_response_observations": len(independent_signatures),
            "gross_top_of_book_feasible_markouts": len(gross_feasible), "capturable_markouts": 0,
            "timing_misses": sum(row.get("valid_timing") is not True for row in markouts),
            "provider_unavailable_markouts": sum(row.get("status") == "provider_unavailable" for row in markouts),
            "expected_markout_horizons": expected, "pending_markout_horizons": max(0, expected - len(markouts)),
            "aggregate_subgroups": len(aggregates),
            "subgroups_meeting_minimum_sample": sum(row.get("minimum_sample_met") is True for row in aggregates),
            "subgroups_allowed_to_claim_edge": 0,
        },
        "breakdowns": {
            "events_by_source": count_by(events, "source"),
            "events_by_type": count_by(events, "event_type"),
            "events_by_timestamp_precision": count_by(events, "source_timestamp_precision"),
            "states_by_provider": count_by(snapshots, "provider"),
            "states_by_quality": count_by(snapshots, "quote_quality"),
            "markouts_by_window": count_by(markouts, "window"),
            "markouts_by_status": count_by(markouts, "status"),
        },
        "latency_ms": {
            name: distribution([value for event in events if (value := as_number((event.get("latency_ms") or {}).get(name))) is not None])
            for name in ("source_to_first_seen", "request_to_response", "response_to_parse", "fetch_to_parse", "parse_to_decision", "first_seen_to_decision")
        },
        "blockers": blockers, "aggregates": aggregates,
        "guardrails": {
            "minimum_independent_gross_sample": MIN_SAMPLE,
            "minimum_sample_is_not_edge_proof": True,
            "yahoo_bars_are_executable": False,
            "gross_top_of_book_is_net_profit": False,
            "prior_v1_rows_are_excluded": True,
        },
    }


def markdown_report(report: dict[str, Any]) -> str:
    counts, breakdowns = report["counts"], report["breakdowns"]
    lines = [
        "# Event Evidence Report — causal v2", "", f"Generated: `{report['generated_at']}`", "",
        "## Status", "", f"- Conclusion: **{report['conclusion']}**", "- Paper only: **yes**",
        "- Profitability claim: **no**", "- Edge claim allowed: **no**",
        "- Prior v1 nearest-bar measurements: **invalidated and excluded**",
        f"- Events: **{counts['events']}**", f"- Valid causal baselines: **{counts['valid_baselines']}**",
        f"- Valid event-row markouts: **{counts['valid_market_response_markouts']}**",
        f"- Independent valid observations: **{counts['independent_valid_response_observations']}**",
        f"- Gross top-of-book feasible markouts: **{counts['gross_top_of_book_feasible_markouts']}**",
        f"- Net capturable markouts: **{counts['capturable_markouts']}**", "",
        "## Evidence rules", "",
        "1. Baselines use only completed bars available at or before decision time.",
        "2. Markouts use only completed bars beginning at or after the intended horizon.",
        "3. Yahoo bars are market-response proxies, never executable quotes.",
        "4. CLOB feasibility requires a predeclared side/quantity, same token, positive displayed size, and valid timing.",
        "5. Gross top-of-book feasibility is not net capture: fees, slippage, queue position, fill probability, and settlement remain unmodeled.",
        "", "## Current blockers", "",
    ]
    lines.extend(f"- {item}" for item in report["blockers"])
    lines += ["", "## Event breakdown", "", "### Source", ""]
    lines.extend(f"- {key}: {value}" for key, value in breakdowns["events_by_source"].items())
    lines += ["", "### Timestamp precision", ""]
    lines.extend(f"- {key}: {value}" for key, value in breakdowns["events_by_timestamp_precision"].items())
    lines += ["", "## Markout subgroups", "",
              "| Source | Event | Precision | Asset | Target | Window | Event rows | Independent n | Gross feasible n | Minimum sample met | Edge claim |",
              "|---|---|---|---|---|---:|---:|---:|---:|---|---|"]
    for row in report["aggregates"]:
        lines.append(
            f"| {row['source']} | {row['event_type']} | {row['timestamp_precision']} | {row['asset_type']} | "
            f"{row['target']} | {row['window']} | {row['valid_event_rows']} | {row['independent_observations']} | "
            f"{row['gross_top_of_book']['n']} | {'yes' if row['minimum_sample_met'] else 'no'} | no |"
        )
    lines += ["", "## Guardrail", "",
              f"Reaching **{MIN_SAMPLE} independent gross observations** is only a sample-size checkpoint; it never automatically authorizes an edge or profitability claim.", ""]
    return "\n".join(lines)


def write_csv(path: Path, aggregates: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["source", "event_type", "timestamp_precision", "asset_type", "target", "window",
              "event_rows", "valid_event_rows", "independent_observations", "market_response_n",
              "market_response_mean", "gross_top_of_book_n", "gross_top_of_book_mean",
              "minimum_sample_met", "edge_claim_allowed"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in aggregates:
            writer.writerow({
                **{key: row.get(key) for key in fields if key in row},
                "market_response_n": row["market_response"]["n"],
                "market_response_mean": row["market_response"]["mean"],
                "gross_top_of_book_n": row["gross_top_of_book"]["n"],
                "gross_top_of_book_mean": row["gross_top_of_book"]["mean"],
            })


def load_current(path: Path) -> list[dict[str, Any]]:
    return [row for row in event_ledger.read_jsonl(path) if row.get("measurement_version") == MEASUREMENT_VERSION]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the causal v2 event evidence report")
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--states", type=Path, default=DEFAULT_STATES)
    parser.add_argument("--markouts", type=Path, default=DEFAULT_MARKOUTS)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MD)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        report = build_report(load_current(args.events), load_current(args.states), load_current(args.markouts))
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        args.markdown.write_text(markdown_report(report), encoding="utf-8")
        write_csv(args.csv, report["aggregates"])
    except Exception as exc:
        print(f"evidence report error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(event_ledger.canonical_json({"measurement_version": MEASUREMENT_VERSION,
          "conclusion": report["conclusion"], "counts": report["counts"],
          "json": str(args.json), "markdown": str(args.markdown), "csv": str(args.csv)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
