from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Mapping


class MarketValidationError(ValueError):
    pass


@dataclass(frozen=True)
class MarketIdentity:
    market_id: str
    condition_id: str
    slug: str
    question: str
    asset: str
    duration_seconds: int
    published_at: str | None
    prediction_start: str
    prediction_end: str
    publication_lead_ms: int | None
    up_token_id: str
    down_token_id: str
    fees_enabled: bool
    gamma_fee_schedule: dict[str, Any]
    verified_at: str | None
    stage: str = "GAMMA_VERIFIED"
    minimum_tick_size: Decimal | None = None
    minimum_order_size: Decimal | None = None


def _array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise MarketValidationError("invalid JSON array") from exc
        if isinstance(parsed, list):
            return parsed
    raise MarketValidationError("expected array or JSON-encoded array")


def _utc(value: Any, name: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise MarketValidationError(f"missing {name}")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketValidationError(f"invalid {name}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _asset(question: str, slug: str) -> str:
    text = f"{question} {slug}".lower()
    btc = bool(re.search(r"\bbitcoin\b|\bbtc-updown\b", text))
    eth = bool(re.search(r"\bethereum\b|\beth-updown\b", text))
    if btc == eth:
        raise MarketValidationError("ambiguous or unsupported underlying")
    return "BTC" if btc else "ETH"


def normalize_gamma_market(row: Mapping[str, Any], *, verified_at: str | None = None) -> MarketIdentity:
    question = str(row.get("question") or "").strip()
    slug = str(row.get("slug") or "").strip()
    condition_id = str(row.get("conditionId") or "").strip()
    market_id = str(row.get("id") or "").strip()
    if not all((question, slug, condition_id, market_id)):
        raise MarketValidationError("missing Gamma identity")
    asset = _asset(question, slug)
    start = _utc(row.get("eventStartTime"), "eventStartTime")
    end = _utc(row.get("endDate"), "endDate")
    duration = int((end - start).total_seconds())
    if duration not in {300, 900}:
        raise MarketValidationError(f"unsupported prediction duration: {duration}")
    duration_slug = re.search(r"-(5m|15m)-", slug.lower())
    expected = "5m" if duration == 300 else "15m"
    if not duration_slug or duration_slug.group(1) != expected:
        raise MarketValidationError("slug duration conflicts with eventStartTime/endDate")
    if "up or down" not in question.lower():
        raise MarketValidationError("question is not an Up/Down market")
    outcomes = [str(x).strip() for x in _array(row.get("outcomes"))]
    tokens = [str(x).strip() for x in _array(row.get("clobTokenIds"))]
    if len(outcomes) != 2 or len(tokens) != 2 or set(outcomes) != {"Up", "Down"}:
        raise MarketValidationError("requires exactly Up and Down outcomes")
    mapping = dict(zip(outcomes, tokens, strict=True))
    if not mapping["Up"] or not mapping["Down"] or mapping["Up"] == mapping["Down"]:
        raise MarketValidationError("invalid outcome-token mapping")
    if not bool(row.get("enableOrderBook")):
        raise MarketValidationError("order book disabled")
    if bool(row.get("closed")):
        raise MarketValidationError("market closed")
    published = str(row.get("startDate") or "").strip() or None
    lead = None
    if published:
        lead = round((start - _utc(published, "startDate")).total_seconds() * 1000)
    fee_schedule = row.get("feeSchedule") if isinstance(row.get("feeSchedule"), dict) else {}
    return MarketIdentity(
        market_id=market_id,
        condition_id=condition_id,
        slug=slug,
        question=question,
        asset=asset,
        duration_seconds=duration,
        published_at=published,
        prediction_start=start.isoformat().replace("+00:00", "Z"),
        prediction_end=end.isoformat().replace("+00:00", "Z"),
        publication_lead_ms=lead,
        up_token_id=mapping["Up"],
        down_token_id=mapping["Down"],
        fees_enabled=bool(row.get("feesEnabled")),
        gamma_fee_schedule=dict(fee_schedule),
        verified_at=verified_at,
    )


def verify_clob_identity(market: MarketIdentity, payload: Mapping[str, Any]) -> MarketIdentity:
    condition = str(payload.get("condition_id") or "")
    if condition != market.condition_id:
        raise MarketValidationError("CLOB condition mismatch")
    tokens_raw = payload.get("tokens")
    if not isinstance(tokens_raw, list) or len(tokens_raw) != 2:
        return replace(market, stage="CLOB_PENDING")
    mapping = {
        str(item.get("outcome") or ""): str(item.get("token_id") or "")
        for item in tokens_raw if isinstance(item, Mapping)
    }
    if mapping != {"Up": market.up_token_id, "Down": market.down_token_id}:
        raise MarketValidationError("CLOB outcome-token mapping mismatch")
    if not bool(payload.get("active")) or bool(payload.get("closed")):
        return replace(market, stage="CLOB_PENDING")
    tick = payload.get("minimum_tick_size")
    minimum = payload.get("minimum_order_size")
    return replace(
        market,
        stage="IDENTITY_VERIFIED",
        minimum_tick_size=Decimal(str(tick)) if tick is not None else None,
        minimum_order_size=Decimal(str(minimum)) if minimum is not None else None,
    )
