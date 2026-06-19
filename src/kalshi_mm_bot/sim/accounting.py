from __future__ import annotations

from dataclasses import dataclass, field

from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.price import COUNT_SCALE, PRICE_SCALE, format_count_fp
from kalshi_mm_bot.market.types import MarketTicker
from kalshi_mm_bot.sim.fills import SimulatedFill


CASH_SCALE = PRICE_SCALE * COUNT_SCALE


@dataclass(slots=True)
class SimPortfolio:
    positions: dict[MarketTicker, int] = field(default_factory=dict)
    cash: dict[MarketTicker, int] = field(default_factory=dict)
    volume: dict[MarketTicker, int] = field(default_factory=dict)

    def position(self, market_ticker: MarketTicker) -> int:
        return self.positions.get(market_ticker, 0)

    def cash_value(self, market_ticker: MarketTicker) -> int:
        return self.cash.get(market_ticker, 0)

    def apply_fill(self, fill: SimulatedFill) -> None:
        signed_count = fill.count if fill.action == "buy" else -fill.count
        signed_cash = -fill.yes_price * fill.count if fill.action == "buy" else fill.yes_price * fill.count

        self.positions[fill.market_ticker] = self.position(fill.market_ticker) + signed_count
        self.cash[fill.market_ticker] = self.cash_value(fill.market_ticker) + signed_cash
        self.volume[fill.market_ticker] = self.volume.get(fill.market_ticker, 0) + fill.count

    def total_cash(self) -> int:
        return sum(self.cash.values())

    def total_position_count(self) -> int:
        return sum(self.positions.values())

    def total_volume_count(self) -> int:
        return sum(self.volume.values())

    def mark_to_market(self, orderbooks: dict[MarketTicker, Orderbook]) -> int:
        value = self.total_cash()

        for ticker, position in self.positions.items():
            book = orderbooks.get(ticker)
            mid = _mid_price(book)

            if mid is not None:
                value += position * mid

        return value


def format_money_value(value: int) -> str:
    sign = "-" if value < 0 else ""
    value = abs(value)
    whole = value // CASH_SCALE
    frac = value % CASH_SCALE
    return f"{sign}{whole}.{frac:0{len(str(CASH_SCALE)) - 1}d}"


def format_contract_count(count: int) -> str:
    return format_count_fp(count)


def _mid_price(book: Orderbook | None) -> int | None:
    if book is None or book.best_bid is None or book.best_ask is None:
        return None

    return (book.best_bid + book.best_ask) // 2
