from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

D = Decimal


@dataclass(frozen=True)
class PartialPairAccounting:
    matched_quantity: Decimal
    unmatched_first_quantity: Decimal
    unmatched_second_quantity: Decimal
    first_paired_cost: Decimal
    second_paired_cost: Decimal
    locked_pair_margin: Decimal
    pair_payout: Decimal
    first_directional_cost: Decimal
    second_directional_cost: Decimal


def account_partial_pair(*, first_quantity: Decimal, first_cost: Decimal, first_fee: Decimal, second_quantity: Decimal, second_cost: Decimal, second_fee: Decimal) -> PartialPairAccounting:
    q1, q2 = D(first_quantity), D(second_quantity)
    if min(q1, q2, D(first_cost), D(second_cost), D(first_fee), D(second_fee)) < 0:
        raise ValueError("quantities and costs must be nonnegative")
    matched = min(q1, q2)
    total1 = D(first_cost) + D(first_fee)
    total2 = D(second_cost) + D(second_fee)
    paired1 = total1 * matched / q1 if q1 else D("0")
    paired2 = total2 * matched / q2 if q2 else D("0")
    return PartialPairAccounting(
        matched_quantity=matched,
        unmatched_first_quantity=q1 - matched,
        unmatched_second_quantity=q2 - matched,
        first_paired_cost=paired1,
        second_paired_cost=paired2,
        locked_pair_margin=matched - paired1 - paired2,
        pair_payout=matched,
        first_directional_cost=total1 - paired1,
        second_directional_cost=total2 - paired2,
    )
