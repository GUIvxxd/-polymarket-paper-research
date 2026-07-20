from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from typing import Any, Mapping

D = Decimal
FIVE_DP = D("0.00001")


@dataclass(frozen=True)
class FeeMetadata:
    fees_enabled: bool
    gamma_rate: Decimal | None
    gamma_exponent: int | None
    gamma_taker_only: bool | None
    up_base_fee_bps: int | None
    down_base_fee_bps: int | None

    @classmethod
    def from_sources(cls, *, fees_enabled: bool, gamma_schedule: Mapping[str, Any], up_base_fee: Any, down_base_fee: Any):
        rate = gamma_schedule.get("rate")
        exponent = gamma_schedule.get("exponent")
        taker = gamma_schedule.get("takerOnly")
        return cls(
            fees_enabled=bool(fees_enabled),
            gamma_rate=D(str(rate)) if rate is not None else None,
            gamma_exponent=int(exponent) if exponent is not None else None,
            gamma_taker_only=bool(taker) if taker is not None else None,
            up_base_fee_bps=int(up_base_fee) if up_base_fee is not None else None,
            down_base_fee_bps=int(down_base_fee) if down_base_fee is not None else None,
        )


@dataclass(frozen=True)
class FeeGate:
    gamma_curve_supported: bool
    token_order_fee_consistent: bool
    economic_eligible: bool


def fee_gate(meta: FeeMetadata) -> FeeGate:
    gamma_ok = (
        meta.fees_enabled
        and meta.gamma_rate is not None
        and meta.gamma_exponent == 1
        and meta.gamma_taker_only is True
    )
    token_ok = (
        meta.up_base_fee_bps is not None
        and meta.down_base_fee_bps is not None
        and meta.up_base_fee_bps == meta.down_base_fee_bps
    )
    return FeeGate(gamma_ok, token_ok, gamma_ok and token_ok)


@dataclass(frozen=True)
class FeeResult:
    raw: Decimal
    documented: Decimal
    conservative: Decimal


def calculate_fee(quantity: Decimal, price: Decimal, rate: Decimal) -> FeeResult:
    quantity, price, rate = D(quantity), D(price), D(rate)
    if quantity < 0 or rate < 0 or price < 0 or price > 1:
        raise ValueError("invalid fee input")
    raw = quantity * rate * price * (D("1") - price)
    documented = raw.quantize(FIVE_DP, rounding=ROUND_HALF_UP)
    conservative = raw.quantize(FIVE_DP, rounding=ROUND_CEILING)
    return FeeResult(raw=raw, documented=documented, conservative=conservative)


def calculate_levelwise(levels: list[tuple[Decimal, Decimal]], rate: Decimal) -> FeeResult:
    parts = [calculate_fee(quantity, price, rate) for price, quantity in levels]
    raw = sum((part.raw for part in parts), D("0"))
    documented = sum((part.documented for part in parts), D("0"))
    conservative = sum((part.conservative for part in parts), D("0"))
    return FeeResult(raw, documented, conservative)
