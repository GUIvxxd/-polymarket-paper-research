from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Iterable, Mapping

D = Decimal


class PairState(StrEnum):
    PAIR_VALID = "PAIR_VALID"
    PAIR_FRESH = "PAIR_FRESH"
    PAIR_SAME_FRAME = "PAIR_SAME_FRAME"


@dataclass
class TokenBook:
    token_id: str
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    has_snapshot: bool = False
    last_frame_index: int | None = None
    last_received_monotonic_ns: int | None = None
    provider_timestamp_ms: int | None = None

    def canonical(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "bids": [[str(p), str(self.bids[p])] for p in sorted(self.bids, reverse=True)],
            "asks": [[str(p), str(self.asks[p])] for p in sorted(self.asks)],
            "has_snapshot": self.has_snapshot,
            "last_frame_index": self.last_frame_index,
        }


@dataclass(frozen=True)
class PairObservation:
    state: PairState
    condition_id: str
    frame_index: int
    received_monotonic_ns: int
    up_best_ask: Decimal | None
    down_best_ask: Decimal | None
    up_book_age_ms: Decimal
    down_book_age_ms: Decimal
    pair_receive_skew_ms: Decimal
    same_parent_frame: bool


@dataclass(frozen=True)
class DepthFill:
    requested_quantity: Decimal
    filled_quantity: Decimal
    cost: Decimal
    vwap: Decimal | None
    levels: tuple[tuple[Decimal, Decimal], ...]


def walk_asks(levels: Iterable[tuple[Decimal, Decimal]], requested_quantity: Decimal) -> DepthFill:
    requested = D(requested_quantity)
    if requested < 0:
        raise ValueError("requested quantity must be nonnegative")
    remaining = requested
    used: list[tuple[Decimal, Decimal]] = []
    for price, size in sorted(((D(p), D(s)) for p, s in levels), key=lambda x: x[0]):
        if price < 0 or price > 1 or size < 0:
            raise ValueError("invalid level")
        if remaining <= 0:
            break
        take = min(size, remaining)
        if take:
            used.append((price, take))
            remaining -= take
    filled = requested - remaining
    cost = sum((price * quantity for price, quantity in used), D("0"))
    return DepthFill(requested, filled, cost, cost / filled if filled else None, tuple(used))


class BookReducer:
    def __init__(self, condition_id: str, up_token_id: str, down_token_id: str, *, fresh_ms: int = 1_000):
        self.condition_id = condition_id
        self.up_token_id = up_token_id
        self.down_token_id = down_token_id
        self.books = {up_token_id: TokenBook(up_token_id), down_token_id: TokenBook(down_token_id)}
        self.fresh_ns = int(fresh_ms * 1_000_000)
        self.gap_open = False
        self.connection_epoch = 1
        # The raw archive is the observation history. Keeping every derived
        # PairObservation here grows by hundreds of bytes per market update
        # and can OOM a long-running collector. Retain only constant-memory
        # operational state; historical observations are reconstructed by
        # replaying the durable raw log.
        self.observation_count = 0
        self.latest_observation: PairObservation | None = None

    @property
    def pair_valid(self) -> bool:
        return not self.gap_open and all(book.has_snapshot for book in self.books.values())

    def begin_epoch(self, epoch: int) -> None:
        self.connection_epoch = epoch
        self.gap_open = False
        self.books = {self.up_token_id: TokenBook(self.up_token_id), self.down_token_id: TokenBook(self.down_token_id)}

    def open_gap(self, reason: str) -> None:
        self.gap_open = True
        for book in self.books.values():
            book.has_snapshot = False

    def apply(self, message: Mapping[str, Any], *, frame_index: int, received_monotonic_ns: int) -> PairObservation | None:
        if self.gap_open:
            return None
        if str(message.get("market") or "") != self.condition_id:
            return None
        event_type = str(message.get("event_type") or "")
        updated: set[str] = set()
        if event_type == "book":
            token = str(message.get("asset_id") or "")
            if token not in self.books:
                return None
            book = self.books[token]
            book.bids = self._levels(message.get("bids"))
            book.asks = self._levels(message.get("asks"))
            book.has_snapshot = True
            self._touch(book, message, frame_index, received_monotonic_ns)
            updated.add(token)
        elif event_type == "price_change":
            changes = message.get("price_changes")
            if not isinstance(changes, list):
                return None
            pending: list[tuple[TokenBook, str, Decimal, Decimal]] = []
            for change in changes:
                if not isinstance(change, Mapping):
                    return None
                token = str(change.get("asset_id") or "")
                if token not in self.books or not self.books[token].has_snapshot:
                    return None
                side = str(change.get("side") or "").upper()
                if side not in {"BUY", "SELL"}:
                    return None
                price, size = D(str(change.get("price"))), D(str(change.get("size")))
                if price < 0 or price > 1 or size < 0:
                    return None
                pending.append((self.books[token], side, price, size))
            for book, side, price, size in pending:
                levels = book.bids if side == "BUY" else book.asks
                if size == 0:
                    levels.pop(price, None)
                else:
                    levels[price] = size
                self._touch(book, message, frame_index, received_monotonic_ns)
                updated.add(book.token_id)
        else:
            return None
        if not self.pair_valid:
            return None
        up, down = self.books[self.up_token_id], self.books[self.down_token_id]
        up_age = received_monotonic_ns - int(up.last_received_monotonic_ns or received_monotonic_ns)
        down_age = received_monotonic_ns - int(down.last_received_monotonic_ns or received_monotonic_ns)
        skew = abs(int(up.last_received_monotonic_ns or 0) - int(down.last_received_monotonic_ns or 0))
        same = updated == {self.up_token_id, self.down_token_id}
        if same:
            state = PairState.PAIR_SAME_FRAME
        elif max(up_age, down_age, skew) <= self.fresh_ns:
            state = PairState.PAIR_FRESH
        else:
            state = PairState.PAIR_VALID
        observation = PairObservation(
            state=state,
            condition_id=self.condition_id,
            frame_index=frame_index,
            received_monotonic_ns=received_monotonic_ns,
            up_best_ask=min(up.asks) if up.asks else None,
            down_best_ask=min(down.asks) if down.asks else None,
            up_book_age_ms=D(up_age) / D("1000000"),
            down_book_age_ms=D(down_age) / D("1000000"),
            pair_receive_skew_ms=D(skew) / D("1000000"),
            same_parent_frame=same,
        )
        self.observation_count += 1
        self.latest_observation = observation
        return observation

    def canonical_hash(self) -> str:
        payload = {token: self.books[token].canonical() for token in sorted(self.books)}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _levels(value: Any) -> dict[Decimal, Decimal]:
        if not isinstance(value, list):
            raise ValueError("book levels must be a list")
        result: dict[Decimal, Decimal] = {}
        for item in value:
            if not isinstance(item, Mapping):
                raise ValueError("invalid level")
            price, size = D(str(item.get("price"))), D(str(item.get("size")))
            if price < 0 or price > 1 or size < 0:
                raise ValueError("invalid level")
            if size:
                result[price] = size
        return result

    @staticmethod
    def _touch(book: TokenBook, message: Mapping[str, Any], frame_index: int, received_ns: int) -> None:
        book.last_frame_index = frame_index
        book.last_received_monotonic_ns = received_ns
        try:
            book.provider_timestamp_ms = int(message.get("timestamp"))
        except (TypeError, ValueError):
            book.provider_timestamp_ms = None
