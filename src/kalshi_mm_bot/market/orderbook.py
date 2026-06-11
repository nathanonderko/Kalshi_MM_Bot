from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cache

from kalshi_mm_bot.market.price import ONE_DOLLAR, parse_count_fp, parse_price_fp
from kalshi_mm_bot.market.types import BookSide, PriceRange


class OrderbookError(Exception):
    pass


class SequenceGapError(OrderbookError):
    pass


class NegativeLevelError(OrderbookError):
    pass


@dataclass(slots=True)
class Orderbook:
    market_ticker: str
    seq: int
    bids: list[int]
    asks: list[int]
    price_levels: tuple[int, ...]
    best_bid: int | None = None
    best_ask: int | None = None

    @classmethod
    def from_snapshot(
        cls,
        market_ticker: str,
        seq: int,
        bids_raw: Iterable[Sequence[str]],
        asks_raw: Iterable[Sequence[str]],
        price_ranges: tuple[PriceRange, ...],
    ) -> "Orderbook":
        bids = [0] * (ONE_DOLLAR + 1)
        asks = [0] * (ONE_DOLLAR + 1)
        best_bid: int | None = None
        best_ask: int | None = None

        for price_text, size_text in bids_raw:
            price = parse_price_fp(price_text)
            size = parse_count_fp(size_text)
            bids[price] = size

            if size > 0 and (best_bid is None or price > best_bid):
                best_bid = price

        for price_text, size_text in asks_raw:
            price = parse_price_fp(price_text)
            size = parse_count_fp(size_text)
            asks[price] = size

            if size > 0 and (best_ask is None or price < best_ask):
                best_ask = price

        return cls(
            market_ticker=market_ticker,
            seq=seq,
            bids=bids,
            asks=asks,
            price_levels=_build_price_levels(price_ranges),
            best_bid=best_bid,
            best_ask=best_ask,
        )

    def apply_delta(
        self,
        *,
        seq: int,
        side: BookSide,
        price: int,
        delta: int,
    ) -> None:
        levels = self.bids if side == "bid" else self.asks
        new_size = levels[price] + delta

        if new_size < 0:
            raise NegativeLevelError(f"{side} level {price} became negative: {new_size}")

        levels[price] = new_size

        if side == "bid":
            self._update_best_bid(price, new_size)
        else:
            self._update_best_ask(price, new_size)

        self.seq = seq

    def _update_best_bid(self, price: int, new_size: int) -> None:
        if new_size > 0:
            if self.best_bid is None or price > self.best_bid:
                self.best_bid = price
            return

        if price == self.best_bid:
            self.best_bid = self._find_next_bid(price)

    def _update_best_ask(self, price: int, new_size: int) -> None:
        if new_size > 0:
            if self.best_ask is None or price < self.best_ask:
                self.best_ask = price
            return

        if price == self.best_ask:
            self.best_ask = self._find_next_ask(price)

    def _find_next_bid(self, price: int) -> int | None:
        index = bisect_left(self.price_levels, price) - 1

        while index >= 0:
            level_price = self.price_levels[index]

            if self.bids[level_price] > 0:
                return level_price

            index -= 1

        return None

    def _find_next_ask(self, price: int) -> int | None:
        index = bisect_right(self.price_levels, price)

        while index < len(self.price_levels):
            level_price = self.price_levels[index]

            if self.asks[level_price] > 0:
                return level_price

            index += 1

        return None


@cache
def _build_price_levels(price_ranges: tuple[PriceRange, ...]) -> tuple[int, ...]:
    levels: set[int] = set()

    for price_range in price_ranges:
        if price_range.step <= 0:
            raise ValueError(f"price range step must be positive: {price_range}")

        levels.update(range(price_range.start, price_range.end + 1, price_range.step))

    if not levels:
        raise ValueError("market metadata has no price levels")

    return tuple(sorted(levels))
