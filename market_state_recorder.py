#!/usr/bin/env python3
"""Capture causal, versioned stock and Polymarket market-state evidence.

Yahoo chart bars are non-executable proxies. A decision baseline may use only a
fully completed bar available at or before the decision. A later markout may use
only a fully completed bar whose start is at or after the requested horizon.
Polymarket books are executable-at-capture only when both sides and positive
sizes exist; late captures are never represented as decision-time books.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import event_ledger

ROOT = Path(__file__).resolve().parent
MEASUREMENT_VERSION = "event_evidence_v2_2026_07_17"
DEFAULT_EVENTS = ROOT / "reports" / "event_evidence_ledger_v2.jsonl"
DEFAULT_STATES = ROOT / "reports" / "event_market_states_v2.jsonl"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
UA = "Hermes-Event-Evidence-Pipeline/2.0 (paper-research; public-data-only)"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def to_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def get_json(url: str, params: dict[str, Any] | None = None, timeout: float = 20.0) -> Any:
    if params:
        url += "?" + urlencode(params, doseq=True)
    request = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def parse_utc(value: Any) -> datetime | None:
    return event_ledger.parse_utc(value)


def iso_from_epoch(value: int | float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def interval_seconds(interval: str) -> int:
    known = {"1m": 60, "2m": 120, "5m": 300, "15m": 900, "30m": 1800,
             "60m": 3600, "90m": 5400, "1h": 3600, "1d": 86400}
    if interval not in known:
        raise ValueError(f"unsupported Yahoo interval: {interval}")
    return known[interval]


def baseline_max_lag(interval: str) -> int:
    return {"1m": 300, "2m": 600, "5m": 900, "15m": 2700, "30m": 5400,
            "60m": 10800, "90m": 16200, "1h": 10800, "1d": 259200}.get(interval, 300)


def choose_yahoo_range(target_at: str | None, observed_at: str) -> tuple[str, str]:
    target = parse_utc(target_at)
    observed = parse_utc(observed_at) or datetime.now(UTC)
    if target is None:
        return "1d", "1m"
    age_days = max(0.0, (observed - target).total_seconds() / 86400)
    if age_days <= 5:
        return "5d", "1m"
    if age_days <= 30:
        return "1mo", "5m"
    if age_days <= 365:
        return "1y", "1d"
    return "5y", "1d"


def fetch_yahoo_chart(ticker: str, target_at: str | None, observed_at: str) -> tuple[dict[str, Any], str, str]:
    range_name, interval = choose_yahoo_range(target_at, observed_at)
    payload = get_json(
        f"{YAHOO_CHART}/{quote(ticker, safe='')}",
        {"range": range_name, "interval": interval, "includePrePost": "true", "events": "div,splits"},
        timeout=20,
    )
    return payload, range_name, interval


def _session_for_timestamp(meta: dict[str, Any], epoch: int) -> str:
    periods = meta.get("currentTradingPeriod") if isinstance(meta, dict) else None
    if isinstance(periods, dict):
        for name in ("pre", "regular", "post"):
            item = periods.get(name)
            if not isinstance(item, dict):
                continue
            try:
                if int(item.get("start")) <= epoch < int(item.get("end")):
                    return {"pre": "premarket", "regular": "regular", "post": "afterhours"}[name]
            except (TypeError, ValueError):
                continue
    return "unknown"


def _value_at(values: Any, index: int) -> float | None:
    if not isinstance(values, list) or index >= len(values):
        return None
    return to_float(values[index])


def stock_state_from_yahoo(
    payload: dict[str, Any], *, ticker: str, target_at: str | None, observed_at: str,
    interval: str = "1m", selection_mode: str = "baseline_asof",
) -> dict[str, Any]:
    chart = payload.get("chart") if isinstance(payload, dict) else None
    result_list = chart.get("result") if isinstance(chart, dict) else None
    result = result_list[0] if isinstance(result_list, list) and result_list else None
    if not isinstance(result, dict):
        error = chart.get("error") if isinstance(chart, dict) else None
        raise ValueError(f"Yahoo chart has no result: {error}")
    timestamps = result.get("timestamp")
    if not isinstance(timestamps, list) or not timestamps:
        raise ValueError("Yahoo chart result has no timestamps")
    indicators = result.get("indicators") if isinstance(result.get("indicators"), dict) else {}
    quotes = indicators.get("quote") if isinstance(indicators, dict) else None
    quote_row = quotes[0] if isinstance(quotes, list) and quotes and isinstance(quotes[0], dict) else {}
    seconds = interval_seconds(interval)
    bars: list[tuple[int, int, int, float]] = []
    for index, raw in enumerate(timestamps):
        try:
            start = int(raw)
        except (TypeError, ValueError):
            continue
        close = _value_at(quote_row.get("close"), index)
        if close is not None:
            bars.append((index, start, start + seconds, close))
    if not bars:
        raise ValueError("Yahoo chart has no usable close bars")

    target = parse_utc(target_at)
    observed = parse_utc(observed_at) or datetime.now(UTC)
    target_epoch = target.timestamp() if target else observed.timestamp()
    observed_epoch = observed.timestamp()
    completed = [bar for bar in bars if bar[2] <= observed_epoch]
    if selection_mode == "baseline_asof":
        candidates = [bar for bar in completed if bar[2] <= target_epoch]
        selected = max(candidates, key=lambda bar: bar[2]) if candidates else None
    elif selection_mode == "markout_at_or_after":
        candidates = [bar for bar in completed if bar[1] >= target_epoch]
        selected = min(candidates, key=lambda bar: bar[1]) if candidates else None
    else:
        raise ValueError(f"unsupported selection_mode: {selection_mode}")
    if selected is None:
        raise ValueError(f"no completed Yahoo bar satisfies {selection_mode} at {target_at}")

    selected_index, bar_start_epoch, bar_end_epoch, close = selected
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    if selection_mode == "baseline_asof":
        lag_seconds = target_epoch - bar_end_epoch
        valid_baseline = 0 <= lag_seconds <= baseline_max_lag(interval)
        quality = ("historical_1m_bar_proxy" if interval == "1m" else f"historical_{interval}_bar_proxy")
        if not valid_baseline:
            quality = "stale_" + quality
    else:
        lag_seconds = bar_end_epoch - target_epoch
        valid_baseline = None
        quality = "historical_1m_bar_proxy" if interval == "1m" else f"historical_{interval}_bar_proxy"
    return {
        "asset_type": "stock",
        "ticker": ticker.upper(),
        "provider": "yahoo_chart_public",
        "provider_symbol": meta.get("symbol") or ticker.upper(),
        "exchange": meta.get("exchangeName"),
        "currency": meta.get("currency"),
        "target_at": target_at,
        "bar_start": iso_from_epoch(bar_start_epoch),
        "bar_end": iso_from_epoch(bar_end_epoch),
        "market_timestamp": iso_from_epoch(bar_end_epoch),
        "observed_at": observed_at,
        "target_offset_seconds": bar_end_epoch - target_epoch,
        "baseline_lag_seconds": lag_seconds if selection_mode == "baseline_asof" else None,
        "selection_mode": selection_mode,
        "valid_baseline": valid_baseline,
        "session": _session_for_timestamp(meta, bar_start_epoch),
        "open": _value_at(quote_row.get("open"), selected_index),
        "high": _value_at(quote_row.get("high"), selected_index),
        "low": _value_at(quote_row.get("low"), selected_index),
        "last": close,
        "volume": _value_at(quote_row.get("volume"), selected_index),
        "bid": None, "ask": None, "bid_size": None, "ask_size": None,
        "spread": None, "mid": None,
        "quote_quality": quality,
        "executable_quote": False,
        "executable_limitations": "Completed public chart bar only; no contemporaneous NBBO bid/ask/depth.",
    }


def _book_levels(value: Any) -> list[tuple[float, float]]:
    if not isinstance(value, list):
        return []
    levels: list[tuple[float, float]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        price, size = to_float(item.get("price")), to_float(item.get("size"))
        if price is not None and size is not None:
            levels.append((price, size))
    return levels


def _provider_timestamp(payload: dict[str, Any]) -> str | None:
    value = payload.get("timestamp") or payload.get("last_trade_price_timestamp")
    number = to_float(value)
    if number is None:
        return None
    if number > 10_000_000_000:
        number /= 1000
    try:
        return iso_from_epoch(number)
    except (OverflowError, OSError, ValueError):
        return None


def polymarket_state_from_book(
    payload: dict[str, Any], *, token_id: str, outcome: str | None, observed_at: str,
    target_at: str | None = None, request_started_at: str | None = None,
) -> dict[str, Any]:
    bids, asks = _book_levels(payload.get("bids")), _book_levels(payload.get("asks"))
    bid = max((price for price, _ in bids), default=None)
    ask = min((price for price, _ in asks), default=None)
    bid_size = next((size for price, size in bids if price == bid), None) if bid is not None else None
    ask_size = next((size for price, size in asks if price == ask), None) if ask is not None else None
    executable = bool(
        bid is not None and ask is not None and 0 <= bid <= ask <= 1
        and bid_size is not None and ask_size is not None and bid_size > 0 and ask_size > 0
    )
    provider_at = _provider_timestamp(payload)
    market_at = provider_at or observed_at
    target_dt, market_dt = parse_utc(target_at), parse_utc(market_at)
    capture_lag = (market_dt - target_dt).total_seconds() if target_dt and market_dt else None
    valid_baseline = capture_lag is not None and 0 <= capture_lag <= 360
    return {
        "asset_type": "polymarket", "token_id": str(token_id), "outcome": outcome,
        "provider": "polymarket_clob_public", "target_at": target_at,
        "request_started_at": request_started_at, "response_received_at": observed_at,
        "provider_timestamp": provider_at, "market_timestamp": market_at, "observed_at": observed_at,
        "capture_lag_seconds": capture_lag, "valid_baseline": valid_baseline,
        "bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size,
        "spread": ask - bid if ask is not None and bid is not None else None,
        "mid": (ask + bid) / 2 if ask is not None and bid is not None else None,
        "last": (ask + bid) / 2 if ask is not None and bid is not None else ask or bid,
        "book_depth_levels": {"bids": len(bids), "asks": len(asks)},
        "quote_quality": "live_public_clob_top_of_book" if executable else "incomplete_public_clob_book",
        "executable_quote": executable,
        "executable_limitations": None if executable else "Both sides, valid prices, and positive displayed top-level sizes are required.",
    }


def polymarket_state_from_decision_book(
    payload: dict[str, Any], *, token_id: str, outcome: str | None, decision_at: str,
    request_started_at: str | None, response_received_at: str | None,
    timing_quality: str | None, provider_timestamp: str | None = None,
) -> dict[str, Any]:
    """Build state from the exact book consumed by a strategy decision.

    A snapshot timestamp alone is retained but cannot establish causal availability.
    Exact request/response timing must show the response arrived before the decision.
    """
    response_at = response_received_at or decision_at
    state = polymarket_state_from_book(
        payload,
        token_id=token_id,
        outcome=outcome,
        observed_at=response_at,
        target_at=decision_at,
        request_started_at=request_started_at,
    )
    decision_dt = parse_utc(decision_at)
    request_dt = parse_utc(request_started_at)
    response_dt = parse_utc(response_received_at)
    age = (decision_dt - response_dt).total_seconds() if decision_dt and response_dt else None
    ordered = bool(
        request_dt and response_dt and decision_dt
        and request_dt <= response_dt <= decision_dt
    )
    valid = bool(timing_quality == "exact_request_response" and ordered and age is not None and age <= 360)
    state.update({
        "snapshot_role": "decision_input_order_book",
        "target_at": decision_at,
        "anchor_at": decision_at,
        "provider_timestamp": provider_timestamp or state.get("provider_timestamp"),
        "market_timestamp": provider_timestamp or response_received_at or state.get("market_timestamp"),
        "observed_at": response_at,
        "response_received_at": response_received_at,
        "decision_input_age_seconds": age,
        "capture_lag_seconds": -age if age is not None else None,
        "timing_quality": timing_quality,
        "valid_baseline": valid,
        "execution_evidence_eligible": bool(valid and state.get("executable_quote")),
        "quote_quality": (
            "causal_decision_input_public_clob_book"
            if valid and state.get("executable_quote")
            else "legacy_or_untimed_strategy_book"
        ),
        "executable_limitations": (
            None
            if valid and state.get("executable_quote")
            else "Exact request <= response <= decision timing and a complete two-sided book are required."
        ),
    })
    return state


def _snapshot_id(event_id: str, target_key: str, role: str = "decision_baseline") -> str:
    digest = hashlib.sha256(f"{MEASUREMENT_VERSION}|{event_id}|{target_key}|{role}".encode()).hexdigest()[:24]
    return f"snv_{digest}"


def _parse_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def select_gamma_market(payload: Any, target: dict[str, Any]) -> dict[str, Any] | None:
    candidates = payload if isinstance(payload, list) else [payload]
    market_id = str(target.get("market_id") or "").strip()
    condition_id = str(target.get("condition_id") or "").strip().lower()
    slug = str(target.get("slug") or "").strip().lower()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if market_id and str(candidate.get("id") or "") != market_id:
            continue
        candidate_condition = str(candidate.get("conditionId") or candidate.get("condition_id") or "").lower()
        if condition_id and candidate_condition != condition_id:
            continue
        if slug and str(candidate.get("slug") or "").lower() != slug:
            continue
        if market_id or condition_id or slug:
            return candidate
    return None


def resolve_polymarket_tokens(target: dict[str, Any]) -> list[dict[str, Any]]:
    has_market_reference = any(target.get(key) for key in ("market_id", "condition_id", "slug"))
    if has_market_reference:
        payload: Any = None
        if target.get("market_id"):
            payload = get_json(f"{GAMMA}/markets/{quote(str(target['market_id']), safe='')}")
        elif target.get("condition_id"):
            payload = get_json(f"{GAMMA}/markets", {"condition_ids": str(target["condition_id"]), "limit": 10})
        elif target.get("slug"):
            payload = get_json(f"{GAMMA}/markets", {"slug": str(target["slug"]), "limit": 10})
        market = select_gamma_market(payload, target)
        if not market:
            return []
        outcomes = [str(value) for value in _parse_list(market.get("outcomes"))]
        token_ids = [str(value) for value in _parse_list(market.get("clobTokenIds"))]
        return [
            {
                "token_id": token,
                "outcome": outcomes[i] if i < len(outcomes) else None,
                "mapping_verified": True,
                "verified_market_id": market.get("id"),
                "verified_condition_id": market.get("conditionId") or market.get("condition_id"),
                "verified_slug": market.get("slug"),
            }
            for i, token in enumerate(token_ids)
        ]
    if target.get("token_id"):
        return [{"token_id": str(target["token_id"]), "outcome": target.get("outcome"), "mapping_verified": False}]
    direct_tokens = _parse_list(target.get("token_ids") or target.get("clobTokenIds"))
    direct_outcomes = _parse_list(target.get("outcomes"))
    return [
        {
            "token_id": str(token),
            "outcome": str(direct_outcomes[i]) if i < len(direct_outcomes) else None,
            "mapping_verified": False,
        }
        for i, token in enumerate(direct_tokens)
    ]


def select_polymarket_tokens(
    target: dict[str, Any], token_targets: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None, str | None, str | None]:
    """Resolve a declared paper direction to exactly one outcome token.

    With no paper side, all tokens may be observed but none is execution-eligible.
    A buy declaration must identify one outcome; buy_yes/buy_no are normalized to
    buying the selected outcome token.
    """
    raw_side = str(target.get("paper_side") or "").strip().lower()
    desired_outcome = str(target.get("paper_outcome") or "").strip().lower() or None
    if not raw_side:
        return token_targets, None, desired_outcome, None
    if raw_side == "buy_yes":
        desired_outcome = "yes"
    elif raw_side == "buy_no":
        desired_outcome = "no"
    elif raw_side != "buy":
        return [], None, desired_outcome, f"unsupported paper_side:{raw_side}"
    if not desired_outcome:
        return [], None, None, "paper_side=buy requires paper_outcome"
    selected = [item for item in token_targets
                if str(item.get("outcome") or "").strip().lower() == desired_outcome]
    if len(selected) != 1:
        return [], None, desired_outcome, f"paper_outcome={desired_outcome} resolved to {len(selected)} tokens"
    if selected[0].get("mapping_verified") is not True:
        return [], None, desired_outcome, "paper outcome token mapping is not authoritatively verified"
    return selected, "buy", desired_outcome, None


def record_market_states(
    events_path: Path, states_path: Path, *, max_events: int = 0,
    event_id: str | None = None, dry_run: bool = False,
) -> dict[str, Any]:
    events = event_ledger.active_events(
        row for row in event_ledger.read_jsonl(events_path)
        if row.get("measurement_version") == MEASUREMENT_VERSION
    )
    if event_id:
        events = [event for event in events if event.get("event_id") == event_id]
    events = [event for event in events if event.get("market_state_eligible", True) is not False]
    events = [event for event in events if (event.get("targets") or {}).get("stock")
              or (event.get("targets") or {}).get("polymarket")]
    if max_events > 0:
        events = events[:max_events]
    existing = {str(row.get("snapshot_id")) for row in event_ledger.read_jsonl(states_path)
                if row.get("measurement_version") == MEASUREMENT_VERSION and row.get("snapshot_id")}
    observed_at = utc_now()
    yahoo_cache: dict[tuple[str, str, str], tuple[dict[str, Any], str, str] | Exception] = {}
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    skipped_existing = 0

    def common(event: dict[str, Any], snapshot_id: str, role: str) -> dict[str, Any]:
        return {
            "schema_version": 2, "measurement_version": MEASUREMENT_VERSION,
            "snapshot_id": snapshot_id, "event_id": event.get("event_id"),
            "logical_event_id": event.get("logical_event_id"), "snapshot_role": role,
            "anchor_at": event.get("decision_at"),
            "eligible_markout_windows": event.get("eligible_markout_windows") or [],
            "source": event.get("source"), "event_type": event.get("event_type"),
            "source_timestamp_precision": event.get("source_timestamp_precision"),
        }

    for event in events:
        event_id_value = str(event.get("event_id"))
        targets = event.get("targets") if isinstance(event.get("targets"), dict) else {}
        stock = targets.get("stock") if isinstance(targets.get("stock"), dict) else None
        if stock and stock.get("ticker"):
            ticker = str(stock["ticker"]).upper()
            snapshot_id = _snapshot_id(event_id_value, f"stock:{ticker}")
            if snapshot_id in existing:
                skipped_existing += 1
            else:
                target_at = event.get("decision_at")
                range_name, interval = choose_yahoo_range(target_at, observed_at)
                cache_key = (ticker, range_name, interval)
                if cache_key not in yahoo_cache:
                    try:
                        yahoo_cache[cache_key] = fetch_yahoo_chart(ticker, target_at, observed_at)
                    except Exception as exc:
                        yahoo_cache[cache_key] = exc
                fetched = yahoo_cache[cache_key]
                try:
                    if isinstance(fetched, Exception):
                        raise fetched
                    payload, _range, actual_interval = fetched
                    state = stock_state_from_yahoo(
                        payload, ticker=ticker, target_at=target_at, observed_at=observed_at,
                        interval=actual_interval, selection_mode="baseline_asof",
                    )
                except Exception as exc:
                    errors.append(f"{event_id_value} stock:{ticker}: {type(exc).__name__}: {exc}")
                else:
                    state.update(common(event, snapshot_id, "decision_asof_completed_bar_proxy"))
                    rows.append(state)
                    existing.add(snapshot_id)

        raw_record = ((event.get("provenance") or {}).get("raw_record")
                      if isinstance(event.get("provenance"), dict) else {})
        strategy_book = raw_record.get("order_book") if isinstance(raw_record, dict) else None
        poly_targets = targets.get("polymarket") if isinstance(targets.get("polymarket"), list) else []
        for poly_target in poly_targets:
            if not isinstance(poly_target, dict):
                continue
            try:
                token_targets = resolve_polymarket_tokens(poly_target)
            except Exception as exc:
                token_targets = []
                errors.append(f"{event_id_value} polymarket resolve: {type(exc).__name__}: {exc}")
            if poly_target.get("observe_exact_token") and poly_target.get("token_id"):
                wanted_token = str(poly_target.get("token_id"))
                exact_targets = [item for item in token_targets if str(item.get("token_id") or "") == wanted_token]
                if len(exact_targets) != 1:
                    errors.append(f"{event_id_value} polymarket exact token {wanted_token} resolved to {len(exact_targets)} mappings")
                token_targets = exact_targets
            token_targets, normalized_side, desired_outcome, selection_error = select_polymarket_tokens(
                poly_target, token_targets
            )
            if selection_error:
                errors.append(f"{event_id_value} polymarket {selection_error}")
            if not token_targets:
                errors.append(f"{event_id_value} polymarket unresolved:{poly_target.get('slug') or poly_target.get('condition_id') or poly_target.get('market_id')}")
            for token_target in token_targets:
                token_id = str(token_target.get("token_id") or "")
                if not token_id:
                    continue
                outcome = token_target.get("outcome")
                role = "decision_input_order_book" if isinstance(strategy_book, dict) else "capture_time_order_book"
                snapshot_id_role = role if isinstance(strategy_book, dict) else "decision_baseline"
                snapshot_id = _snapshot_id(event_id_value, f"polymarket:{token_id}", snapshot_id_role)
                if snapshot_id in existing:
                    skipped_existing += 1
                    continue
                try:
                    if isinstance(strategy_book, dict):
                        state = polymarket_state_from_decision_book(
                            strategy_book,
                            token_id=token_id,
                            outcome=outcome,
                            decision_at=str(event.get("decision_at") or ""),
                            request_started_at=strategy_book.get("request_started_at"),
                            response_received_at=strategy_book.get("response_received_at"),
                            timing_quality=strategy_book.get("timing_quality"),
                            provider_timestamp=strategy_book.get("provider_timestamp"),
                        )
                    else:
                        request_started_at = utc_now()
                        book = get_json(f"{CLOB}/book", {"token_id": token_id}, timeout=15)
                        received_at = utc_now()
                        state = polymarket_state_from_book(
                            book, token_id=token_id, outcome=outcome, observed_at=received_at,
                            target_at=event.get("decision_at"), request_started_at=request_started_at,
                        )
                except Exception as exc:
                    errors.append(f"{event_id_value} polymarket:{token_id}: {type(exc).__name__}: {exc}")
                    continue
                state.update(common(event, snapshot_id, role))
                state["paper_side"] = normalized_side
                state["paper_outcome"] = desired_outcome
                state["paper_quantity"] = to_float(poly_target.get("paper_quantity"))
                state["mapping_verified"] = token_target.get("mapping_verified") is True
                state["execution_evidence_eligible"] = bool(
                    state.get("execution_evidence_eligible", state.get("valid_baseline") is True and state.get("executable_quote"))
                    and state["mapping_verified"]
                )
                state["strategy_name"] = event.get("strategy_name")
                state["decision_action"] = event.get("decision_action")
                state["market_lifecycle_id"] = event.get("market_lifecycle_id")
                state["strategy_position_id"] = event.get("strategy_position_id")
                state["decision_mode"] = event.get("decision_mode")
                state["verified_market_id"] = token_target.get("verified_market_id")
                state["verified_condition_id"] = token_target.get("verified_condition_id")
                state["verified_slug"] = token_target.get("verified_slug")
                rows.append(state)
                existing.add(snapshot_id)

    if rows and not dry_run:
        states_path.parent.mkdir(parents=True, exist_ok=True)
        with states_path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(event_ledger.canonical_json(row) + "\n")
    return {
        "measurement_version": MEASUREMENT_VERSION, "events_considered": len(events),
        "snapshots_created": len(rows),
        "valid_baselines": sum(row.get("valid_baseline") is True for row in rows),
        "executable_at_capture": sum(bool(row.get("executable_quote")) for row in rows),
        "errors_count": len(errors), "errors": errors[:20], "skipped_existing": skipped_existing,
        "states_path": str(states_path), "dry_run": dry_run,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture causal event-linked stock bars and Polymarket order books")
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--states", type=Path, default=DEFAULT_STATES)
    parser.add_argument("--max-events", type=int, default=0)
    parser.add_argument("--event-id")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        result = record_market_states(args.events, args.states, max_events=args.max_events,
                                      event_id=args.event_id, dry_run=args.dry_run)
    except Exception as exc:
        print(f"market state recorder error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(event_ledger.canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
