from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from kalshi_mm_bot.api.feed_controller import FeedController, ORDERBOOK_CHANNEL
from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.view import TopOfBookRow, top_of_book_rows
from kalshi_mm_bot.recording import RecordedRestClient, RecordedWebSocketClient, RecordingSessionReader
from kalshi_mm_bot.sim.accounting import SimPortfolio
from kalshi_mm_bot.sim.fills import FillModel, SimulatedFill
from kalshi_mm_bot.sim.orders import SimulatedOrder
from kalshi_mm_bot.strategy.types import QuoteIntent, Strategy, StrategyContext


@dataclass(frozen=True, slots=True)
class BacktestSummary:
    strategy_name: str
    fill_model: str
    event_count: int
    order_count: int
    open_order_count: int
    fill_count: int
    buy_filled_count: int
    sell_filled_count: int
    position_count: int
    volume_count: int
    cash_value: int
    mark_to_market_value: int


@dataclass(frozen=True, slots=True)
class BacktestUpdate:
    event_count: int
    updated_ticker: str | None
    rows: tuple[TopOfBookRow, ...]
    summary: BacktestSummary
    recent_fills: tuple[SimulatedFill, ...]
    final: bool = False


@dataclass(frozen=True, slots=True)
class BacktestResult:
    recording: Path
    tickers: tuple[str, ...]
    summary: BacktestSummary
    fills: tuple[SimulatedFill, ...]
    orders: tuple[SimulatedOrder, ...]
    final_rows: tuple[TopOfBookRow, ...]


UpdateCallback = Callable[[BacktestUpdate], None]
StopRequested = Callable[[], bool]


class SimulatedOrderManager:
    def __init__(
        self,
        *,
        fill_model: FillModel,
        portfolio: SimPortfolio,
        latency_seconds: float = 0.0,
    ) -> None:
        if latency_seconds < 0:
            raise ValueError("latency_seconds must be non-negative")

        self.fill_model = fill_model
        self.portfolio = portfolio
        self.latency_seconds = latency_seconds
        self.orders: dict[str, SimulatedOrder] = {}
        self.fills: list[SimulatedFill] = []
        self.event_count = 0

        self._next_order_number = 1

    def process_market_event(
        self,
        raw_msg: dict,
        orderbooks: dict[str, Orderbook],
        context: StrategyContext,
    ) -> tuple[SimulatedFill, ...]:
        self.event_count = context.event_count
        self._settle_due_orders(orderbooks, context)
        candidates = self.fill_model.process_event(
            raw_msg,
            orderbooks,
            tuple(order for order in self.orders.values() if order.is_fillable),
            context,
        )
        fills: list[SimulatedFill] = []

        for candidate in candidates:
            order = self.orders.get(candidate.order_id)

            if order is None or not order.is_fillable:
                continue

            count = min(candidate.count, order.remaining_count)

            if count <= 0:
                continue

            fill = candidate if count == candidate.count else replace(candidate, count=count)
            order.remaining_count -= count
            self.portfolio.apply_fill(fill)
            self.fills.append(fill)
            fills.append(fill)

            if order.remaining_count <= 0:
                order.status = "filled"
                self.fill_model.on_order_closed(order)

        return tuple(fills)

    def sync_market_quotes(
        self,
        market_ticker: str,
        intents: Iterable[QuoteIntent],
        orderbooks: dict[str, Orderbook],
        context: StrategyContext,
    ) -> None:
        self._settle_due_orders(orderbooks, context)
        wanted = {intent.quote_id: intent for intent in intents}

        for order in tuple(self.orders.values()):
            if order.market_ticker != market_ticker or not _is_live_order(order):
                continue

            intent = wanted.get(order.quote_id)

            if intent is None or not _matches_intent(order, intent):
                self.cancel_order(order, context)

        for intent in wanted.values():
            if self._has_matching_live_order(intent):
                continue

            self.place_order(intent, orderbooks, context)

    def place_order(
        self,
        intent: QuoteIntent,
        orderbooks: dict[str, Orderbook],
        context: StrategyContext,
    ) -> SimulatedOrder | None:
        book = orderbooks.get(intent.market_ticker)

        if book is None:
            return None

        order = SimulatedOrder.from_intent(
            self._next_order_id(),
            intent,
            now_offset_seconds=context.offset_seconds,
            latency_seconds=self.latency_seconds,
        )
        self.orders[order.order_id] = order
        self._open_if_due(order, book, context)
        return order

    def cancel_order(self, order: SimulatedOrder, context: StrategyContext) -> None:
        if order.status in {"canceled", "filled"}:
            return

        if order.status == "pending_open":
            order.status = "canceled"
            order.canceled_offset_seconds = context.offset_seconds
            return

        cancel_offset = context.offset_seconds + self.latency_seconds

        if cancel_offset <= context.offset_seconds:
            order.status = "canceled"
            order.canceled_offset_seconds = context.offset_seconds
            self.fill_model.on_order_closed(order)
            return

        order.status = "pending_cancel"
        order.canceled_offset_seconds = cancel_offset

    def _settle_due_orders(
        self,
        orderbooks: dict[str, Orderbook],
        context: StrategyContext,
    ) -> None:
        for order in tuple(self.orders.values()):
            if order.status == "pending_open":
                book = orderbooks.get(order.market_ticker)

                if book is not None:
                    self._open_if_due(order, book, context)

            elif (
                order.status == "pending_cancel"
                and order.canceled_offset_seconds is not None
                and order.canceled_offset_seconds <= context.offset_seconds
            ):
                order.status = "canceled"
                self.fill_model.on_order_closed(order)

    def _open_if_due(
        self,
        order: SimulatedOrder,
        book: Orderbook,
        context: StrategyContext,
    ) -> None:
        if order.status != "pending_open":
            return

        if order.active_offset_seconds > context.offset_seconds:
            return

        order.status = "open"
        self.fill_model.on_order_opened(order, book)

    def _has_matching_live_order(self, intent: QuoteIntent) -> bool:
        return any(
            _is_live_order(order)
            and order.status != "pending_cancel"
            and _matches_intent(order, intent)
            for order in self.orders.values()
        )

    def _next_order_id(self) -> str:
        order_id = f"sim-{self._next_order_number}"
        self._next_order_number += 1
        return order_id


async def run_replay_backtest(
    recording: str | Path,
    *,
    strategy: Strategy,
    fill_model: FillModel,
    speed_multiplier: float = 0.0,
    latency_seconds: float = 0.0,
    on_update: UpdateCallback | None = None,
    update_interval_seconds: float = 0.25,
    stop_requested: StopRequested | None = None,
) -> BacktestResult:
    reader = RecordingSessionReader.open(recording)

    if ORDERBOOK_CHANNEL not in reader.manifest.channels:
        raise ValueError("backtests require orderbook_delta recordings")

    ws = RecordedWebSocketClient.from_session(reader, speed_multiplier=speed_multiplier)
    rest = RecordedRestClient(reader.manifest)
    controller = FeedController(rest=rest, ws=ws)
    portfolio = SimPortfolio()
    manager = SimulatedOrderManager(
        fill_model=fill_model,
        portfolio=portfolio,
        latency_seconds=latency_seconds,
    )
    last_update_monotonic = 0.0
    result: BacktestResult | None = None

    try:
        await controller.connect()
        await controller.subscribe(reader.manifest.tickers, channels=(ORDERBOOK_CHANNEL,))

        while True:
            if stop_requested is not None and stop_requested():
                break

            try:
                updated_ticker = await controller.recv()
            except EOFError:
                break

            event = ws.last_event

            if event is None:
                continue

            context = StrategyContext(
                event_count=ws.returned_count,
                offset_seconds=event.offset_seconds,
                observed_at_utc=event.observed_at_utc,
            )
            recent_fills = manager.process_market_event(event.msg, controller.orderbooks, context)

            if updated_ticker is not None:
                book = controller.orderbooks.get(updated_ticker)

                if book is not None:
                    intents = strategy.on_orderbook(context, updated_ticker, book, portfolio)
                    manager.sync_market_quotes(
                        updated_ticker,
                        intents,
                        controller.orderbooks,
                        context,
                    )

            if on_update is not None:
                now = time.monotonic()

                if now - last_update_monotonic >= update_interval_seconds:
                    last_update_monotonic = now
                    on_update(
                        _build_update(
                            reader,
                            strategy,
                            fill_model,
                            manager,
                            controller,
                            recent_fills,
                            updated_ticker=updated_ticker,
                        )
                    )

        result = _build_result(reader, strategy, fill_model, manager, controller)
    finally:
        await controller.close()

    if result is None:
        raise RuntimeError("backtest ended before a result could be built")

    if on_update is not None:
        on_update(
            BacktestUpdate(
                event_count=result.summary.event_count,
                updated_ticker=None,
                rows=result.final_rows,
                summary=result.summary,
                recent_fills=(),
                final=True,
            )
        )

    return result


def _build_update(
    reader: RecordingSessionReader,
    strategy: Strategy,
    fill_model: FillModel,
    manager: SimulatedOrderManager,
    controller: FeedController,
    recent_fills: tuple[SimulatedFill, ...],
    updated_ticker: str | None,
) -> BacktestUpdate:
    summary = _build_summary(strategy, fill_model, manager, controller)
    return BacktestUpdate(
        event_count=summary.event_count,
        updated_ticker=updated_ticker,
        rows=top_of_book_rows(controller.orderbooks, reader.manifest.tickers),
        summary=summary,
        recent_fills=recent_fills,
    )


def _build_result(
    reader: RecordingSessionReader,
    strategy: Strategy,
    fill_model: FillModel,
    manager: SimulatedOrderManager,
    controller: FeedController,
) -> BacktestResult:
    return BacktestResult(
        recording=reader.directory,
        tickers=reader.manifest.tickers,
        summary=_build_summary(strategy, fill_model, manager, controller),
        fills=tuple(manager.fills),
        orders=tuple(manager.orders.values()),
        final_rows=top_of_book_rows(controller.orderbooks, reader.manifest.tickers),
    )


def _build_summary(
    strategy: Strategy,
    fill_model: FillModel,
    manager: SimulatedOrderManager,
    controller: FeedController,
) -> BacktestSummary:
    fills = tuple(manager.fills)
    return BacktestSummary(
        strategy_name=strategy.name,
        fill_model=fill_model.name,
        event_count=manager.event_count,
        order_count=len(manager.orders),
        open_order_count=sum(1 for order in manager.orders.values() if order.is_fillable),
        fill_count=len(fills),
        buy_filled_count=sum(fill.count for fill in fills if fill.action == "buy"),
        sell_filled_count=sum(fill.count for fill in fills if fill.action == "sell"),
        position_count=manager.portfolio.total_position_count(),
        volume_count=manager.portfolio.total_volume_count(),
        cash_value=manager.portfolio.total_cash(),
        mark_to_market_value=manager.portfolio.mark_to_market(controller.orderbooks),
    )


def _is_live_order(order: SimulatedOrder) -> bool:
    return order.status in {"pending_open", "open", "pending_cancel"}


def _matches_intent(order: SimulatedOrder, intent: QuoteIntent) -> bool:
    return (
        order.quote_id == intent.quote_id
        and order.market_ticker == intent.market_ticker
        and order.action == intent.action
        and order.side == intent.side
        and order.yes_price == intent.yes_price
        and order.remaining_count == intent.count
    )
