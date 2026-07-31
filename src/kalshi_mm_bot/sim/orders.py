from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from kalshi_mm_bot.market.types import (
    BookSide,
    MarketTicker,
    OrderAction,
    OrderId,
    OutcomeSide,
    order_book_side,
)
from kalshi_mm_bot.strategy.types import QuoteIntent


OrderStatus = Literal["pending_open", "open", "pending_cancel", "canceled", "filled"]


@dataclass(slots=True)
class SimulatedOrder:
    order_id: OrderId
    quote_id: str
    market_ticker: MarketTicker
    action: OrderAction
    side: OutcomeSide
    yes_price: int
    count: int
    remaining_count: int
    status: OrderStatus
    created_offset_seconds: float
    active_offset_seconds: float
    canceled_offset_seconds: float | None = None

    @classmethod
    def from_intent(
        cls,
        order_id: OrderId,
        intent: QuoteIntent,
        *,
        now_offset_seconds: float,
        latency_seconds: float,
    ) -> "SimulatedOrder":
        return cls(
            order_id=order_id,
            quote_id=intent.quote_id,
            market_ticker=intent.market_ticker,
            action=intent.action,
            side=intent.side,
            yes_price=intent.yes_price,
            count=intent.count,
            remaining_count=intent.count,
            status="pending_open",
            created_offset_seconds=now_offset_seconds,
            active_offset_seconds=now_offset_seconds + latency_seconds,
        )

    @property
    def is_fillable(self) -> bool:
        return self.status in {"open", "pending_cancel"} and self.remaining_count > 0

    @property
    def book_side(self) -> BookSide:
        return order_book_side(self.action, self.side)
