from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.price import parse_count_fp, parse_price_fp
from kalshi_mm_bot.market.types import BookSide, MarketTicker, OrderAction, OrderId, OutcomeSide
from kalshi_mm_bot.sim.orders import SimulatedOrder
from kalshi_mm_bot.strategy.types import StrategyContext


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    fill_id: str
    order_id: OrderId
    market_ticker: MarketTicker
    action: OrderAction
    side: OutcomeSide
    yes_price: int
    count: int
    offset_seconds: float
    observed_at_utc: str | None
    fill_model: str
    reason: str


@dataclass(frozen=True, slots=True)
class OrderbookDelta:
    market_ticker: MarketTicker
    book_side: BookSide
    yes_price: int
    delta_count: int


class FillModel(Protocol):
    name: str

    def on_order_opened(self, order: SimulatedOrder, book: Orderbook) -> None:
        ...

    def on_order_closed(self, order: SimulatedOrder) -> None:
        ...

    def process_event(
        self,
        raw_msg: dict,
        orderbooks: Mapping[str, Orderbook],
        orders: Iterable[SimulatedOrder],
        context: StrategyContext,
    ) -> tuple[SimulatedFill, ...]:
        ...


class OptimisticFillModel:
    name = "optimistic"

    def on_order_opened(self, order: SimulatedOrder, book: Orderbook) -> None:
        del order, book

    def on_order_closed(self, order: SimulatedOrder) -> None:
        del order

    def process_event(
        self,
        raw_msg: dict,
        orderbooks: Mapping[str, Orderbook],
        orders: Iterable[SimulatedOrder],
        context: StrategyContext,
    ) -> tuple[SimulatedFill, ...]:
        delta = parse_orderbook_delta(raw_msg)
        fills: list[SimulatedFill] = []

        for order in orders:
            book = orderbooks.get(order.market_ticker)

            if book is None:
                continue

            if _is_marketable(order, book, strict=False):
                fills.append(_fill(order, order.remaining_count, context, self.name, "cross_or_touch"))
                continue

            if delta is None or delta.market_ticker != order.market_ticker:
                continue

            if _went_through(order, book, delta):
                fills.append(_fill(order, order.remaining_count, context, self.name, "through"))
                continue

            if _same_level_reduction(order, delta):
                fills.append(
                    _fill(
                        order,
                        min(order.remaining_count, -delta.delta_count),
                        context,
                        self.name,
                        "same_level_reduction",
                    )
                )

        return tuple(fills)


class PessimisticFillModel:
    name = "pessimistic"

    def on_order_opened(self, order: SimulatedOrder, book: Orderbook) -> None:
        del order, book

    def on_order_closed(self, order: SimulatedOrder) -> None:
        del order

    def process_event(
        self,
        raw_msg: dict,
        orderbooks: Mapping[str, Orderbook],
        orders: Iterable[SimulatedOrder],
        context: StrategyContext,
    ) -> tuple[SimulatedFill, ...]:
        delta = parse_orderbook_delta(raw_msg)
        fills: list[SimulatedFill] = []

        for order in orders:
            book = orderbooks.get(order.market_ticker)

            if book is None:
                continue

            if _is_marketable(order, book, strict=True):
                fills.append(_fill(order, order.remaining_count, context, self.name, "strict_cross"))
                continue

            if delta is not None and _went_through(order, book, delta):
                fills.append(_fill(order, order.remaining_count, context, self.name, "through"))

        return tuple(fills)


@dataclass(slots=True)
class _QueueState:
    queue_ahead: int


class QueueAwareFillModel:
    name = "queue"

    def __init__(
        self,
        *,
        trade_fraction: float = 0.5,
        fill_on_through: bool = True,
    ) -> None:
        if not 0 <= trade_fraction <= 1:
            raise ValueError("trade_fraction must be between 0 and 1")

        self.trade_fraction = trade_fraction
        self.fill_on_through = fill_on_through
        self._states: dict[OrderId, _QueueState] = {}

    def on_order_opened(self, order: SimulatedOrder, book: Orderbook) -> None:
        levels = book.bids if order.book_side == "bid" else book.asks
        self._states[order.order_id] = _QueueState(queue_ahead=levels[order.yes_price])

    def on_order_closed(self, order: SimulatedOrder) -> None:
        self._states.pop(order.order_id, None)

    def process_event(
        self,
        raw_msg: dict,
        orderbooks: Mapping[str, Orderbook],
        orders: Iterable[SimulatedOrder],
        context: StrategyContext,
    ) -> tuple[SimulatedFill, ...]:
        delta = parse_orderbook_delta(raw_msg)
        fills: list[SimulatedFill] = []

        for order in orders:
            book = orderbooks.get(order.market_ticker)

            if book is None:
                continue

            if _is_marketable(order, book, strict=False):
                fills.append(_fill(order, order.remaining_count, context, self.name, "cross_or_touch"))
                continue

            if delta is None or delta.market_ticker != order.market_ticker:
                continue

            if not _same_level_reduction(order, delta):
                if self.fill_on_through and _went_through(order, book, delta):
                    state = self._states.setdefault(order.order_id, _QueueState(queue_ahead=0))
                    if state.queue_ahead <= 0:
                        fills.append(_fill(order, order.remaining_count, context, self.name, "through"))
                continue

            state = self._states.setdefault(order.order_id, _QueueState(queue_ahead=0))
            effective_reduction = _fractional_count(-delta.delta_count, self.trade_fraction)

            if state.queue_ahead > 0:
                consumed_ahead = min(state.queue_ahead, effective_reduction)
                state.queue_ahead -= consumed_ahead
                effective_reduction -= consumed_ahead

            if effective_reduction > 0:
                fills.append(
                    _fill(
                        order,
                        min(order.remaining_count, effective_reduction),
                        context,
                        self.name,
                        "queue_exhausted",
                    )
                )
            elif self.fill_on_through and state.queue_ahead <= 0 and _went_through(order, book, delta):
                fills.append(_fill(order, order.remaining_count, context, self.name, "queue_through"))

        return tuple(fills)


def parse_orderbook_delta(raw_msg: dict) -> OrderbookDelta | None:
    if raw_msg.get("type") != "orderbook_delta":
        return None

    data = raw_msg["msg"]
    side = data["side"]

    if side == "yes":
        book_side: BookSide = "bid"
    elif side == "no":
        book_side = "ask"
    else:
        raise ValueError(f"unknown orderbook side: {side!r}")

    return OrderbookDelta(
        market_ticker=data["market_ticker"],
        book_side=book_side,
        yes_price=parse_price_fp(data["price_dollars"]),
        delta_count=parse_count_fp(data["delta_fp"]),
    )


def fill_model_from_name(name: str) -> FillModel:
    normalized = name.strip().lower()

    if normalized == "optimistic":
        return OptimisticFillModel()

    if normalized == "pessimistic":
        return PessimisticFillModel()

    if normalized in {"queue", "queue-aware", "queue_aware"}:
        return QueueAwareFillModel()

    raise ValueError(f"unknown fill model: {name!r}")


def _same_level_reduction(order: SimulatedOrder, delta: OrderbookDelta) -> bool:
    return (
        delta.delta_count < 0
        and order.book_side == delta.book_side
        and order.yes_price == delta.yes_price
    )


def _went_through(order: SimulatedOrder, book: Orderbook, delta: OrderbookDelta) -> bool:
    if delta.delta_count >= 0 or order.book_side != delta.book_side:
        return False

    if order.book_side == "bid":
        if delta.yes_price < order.yes_price:
            return False

        return book.best_bid is None or book.best_bid < order.yes_price

    if delta.yes_price > order.yes_price:
        return False

    return book.best_ask is None or book.best_ask > order.yes_price


def _is_marketable(order: SimulatedOrder, book: Orderbook, *, strict: bool) -> bool:
    if order.action == "buy":
        if book.best_ask is None:
            return False

        return book.best_ask < order.yes_price if strict else book.best_ask <= order.yes_price

    if book.best_bid is None:
        return False

    return book.best_bid > order.yes_price if strict else book.best_bid >= order.yes_price


def _fill(
    order: SimulatedOrder,
    count: int,
    context: StrategyContext,
    fill_model: str,
    reason: str,
) -> SimulatedFill:
    return SimulatedFill(
        fill_id=f"{context.event_count}:{order.order_id}",
        order_id=order.order_id,
        market_ticker=order.market_ticker,
        action=order.action,
        side=order.side,
        yes_price=order.yes_price,
        count=count,
        offset_seconds=context.offset_seconds,
        observed_at_utc=context.observed_at_utc,
        fill_model=fill_model,
        reason=reason,
    )


def _fractional_count(count: int, fraction: float) -> int:
    if count <= 0 or fraction <= 0:
        return 0

    result = int(round(count * fraction))

    if result == 0:
        return 1

    return min(count, result)
