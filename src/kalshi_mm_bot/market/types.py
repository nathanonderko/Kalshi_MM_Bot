from dataclasses import dataclass
from typing import Literal, TypeAlias

MarketTicker: TypeAlias = str
OrderId: TypeAlias = str

BookSide = Literal["bid", "ask"]
OutcomeSide = Literal["yes", "no"]
OrderAction = Literal["buy", "sell"]


@dataclass(frozen=True, slots=True)
class PriceRange:
    start: int
    end: int
    step: int


@dataclass(frozen=True, slots=True)
class OrderFill:
    trade_id: str
    order_id: OrderId
    market_ticker: MarketTicker
    action: OrderAction
    side: OutcomeSide
    yes_price: int
    count: int
    post_position: int
    is_taker: bool


@dataclass(frozen=True, slots=True)
class MarketPosition:
    market_ticker: MarketTicker
    position: int
    position_cost: int
    realized_pnl: int
    fees_paid: int
    volume: int


def outcome_side_to_book_side(side: str) -> BookSide:
    if side == "yes":
        return "bid"

    if side == "no":
        return "ask"

    raise ValueError(f"unknown outcome side: {side!r}")


def book_side_to_outcome_side(side: BookSide) -> OutcomeSide:
    return "yes" if side == "bid" else "no"
