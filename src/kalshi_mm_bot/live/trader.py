from __future__ import annotations

import time
from asyncio import TimeoutError
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from kalshi_mm_bot.api.auth import KalshiAuth
from kalshi_mm_bot.api.feed_controller import (
    FILL_CHANNEL,
    MARKET_POSITIONS_CHANNEL,
    ORDERBOOK_CHANNEL,
    USER_ORDERS_CHANNEL,
    FeedController,
)
from kalshi_mm_bot.api.parser import ParsedWsMessage
from kalshi_mm_bot.api.rest import CancelOrderRequest, CreateOrderRequest, KalshiRestClient
from kalshi_mm_bot.api.websocket import KalshiWebSocketClient
from kalshi_mm_bot.config import load_settings
from kalshi_mm_bot.market.price import (
    COUNT_SCALE,
    format_count_fp,
    format_price_fp,
    ONE_DOLLAR,
    parse_count_fp,
    parse_price_fp,
    PRICE_SCALE,
)
from kalshi_mm_bot.market.types import (
    MarketPosition,
    MarketTicker,
    OrderAction,
    OrderFill,
    OutcomeSide,
    order_book_side,
)
from kalshi_mm_bot.strategy.quotes import quote_intent_map
from kalshi_mm_bot.strategy.types import QuoteIntent, Strategy, StrategyContext

StatusCallback = Callable[[str], None]
StopRequested = Callable[[], bool]

TERMINAL_ORDER_STATUSES = {
    "canceled",
    "cancelled",
    "executed",
    "filled",
    "rejected",
}


@dataclass(slots=True)
class LivePortfolio:
    positions: dict[MarketTicker, int] = field(default_factory=dict)

    @classmethod
    async def load(
        cls,
        rest: KalshiRestClient,
        tickers: Iterable[str],
    ) -> "LivePortfolio":
        return cls(positions=await rest.get_positions(tuple(tickers)))

    def position(self, market_ticker: MarketTicker) -> int:
        return self.positions.get(market_ticker, 0)

    def apply_message(self, message: ParsedWsMessage | None) -> None:
        if isinstance(message, OrderFill):
            self.positions[message.market_ticker] = message.post_position
        elif isinstance(message, MarketPosition):
            self.positions[message.market_ticker] = message.position


@dataclass(slots=True)
class LiveOrder:
    order_id: str
    client_order_id: str
    quote_id: str
    market_ticker: MarketTicker
    action: OrderAction
    side: OutcomeSide
    yes_price: int
    remaining_count: int

    @classmethod
    def from_intent(
        cls,
        *,
        order_id: str,
        client_order_id: str,
        intent: QuoteIntent,
        remaining_count: int | None = None,
    ) -> "LiveOrder":
        return cls(
            order_id=order_id,
            client_order_id=client_order_id,
            quote_id=intent.quote_id,
            market_ticker=intent.market_ticker,
            action=intent.action,
            side=intent.side,
            yes_price=intent.yes_price,
            remaining_count=intent.count if remaining_count is None else remaining_count,
        )

    def matches(self, intent: QuoteIntent) -> bool:
        return (
            self.quote_id == intent.quote_id
            and self.market_ticker == intent.market_ticker
            and self.action == intent.action
            and self.side == intent.side
            and self.yes_price == intent.yes_price
            and self.remaining_count == intent.count
        )


@dataclass(slots=True)
class LiveRunStats:
    event_count: int = 0
    orderbook_updates: int = 0
    fill_count: int = 0
    create_count: int = 0
    cancel_count: int = 0
    dry_run: bool = True


class LiveOrderManager:
    def __init__(
        self,
        rest: KalshiRestClient,
        *,
        dry_run: bool = True,
        client_prefix: str = "kmm",
        min_requote_seconds: float = 0.0,
        rejection_cooldown_seconds: float = 1.0,
        status: StatusCallback | None = None,
    ) -> None:
        if min_requote_seconds < 0:
            raise ValueError("min_requote_seconds must be non-negative")

        if rejection_cooldown_seconds < 0:
            raise ValueError("rejection_cooldown_seconds must be non-negative")

        normalized_prefix = client_prefix.strip().rstrip("-")

        if not normalized_prefix:
            raise ValueError("client_prefix must not be empty")

        self.rest = rest
        self.dry_run = dry_run
        self.client_prefix = normalized_prefix
        self.min_requote_seconds = min_requote_seconds
        self.rejection_cooldown_seconds = rejection_cooldown_seconds
        self.status = status
        self.orders: dict[str, LiveOrder] = {}

        self._run_id = uuid4().hex[:8]
        self._next_order_number = 1
        self._last_sync_by_ticker: dict[str, float] = {}
        self._rejected_quote_until: dict[str, float] = {}
        self._recently_canceled_order_ids: set[str] = set()

    async def cancel_stale_bot_orders(
        self,
        tickers: Iterable[str],
        *,
        reason: str = "startup cleanup",
    ) -> int:
        canceled = 0

        for ticker in tickers:
            for raw_order in await self.rest.get_orders(ticker=ticker, status="resting"):
                client_order_id = str(raw_order.get("client_order_id", ""))
                order_id = raw_order.get("order_id")

                if not client_order_id.startswith(self.client_prefix + "-") or not order_id:
                    continue

                if str(order_id) in self._recently_canceled_order_ids:
                    continue

                await self._cancel_order_id(str(order_id), reason=reason)
                canceled += 1

        return canceled

    async def sync_quotes(
        self,
        market_ticker: str,
        intents: Iterable[QuoteIntent],
        *,
        now: float | None = None,
    ) -> tuple[int, int]:
        wanted = quote_intent_map(intents)
        now = time.monotonic() if now is None else now
        last_sync = self._last_sync_by_ticker.get(market_ticker)
        can_create = (
            last_sync is None
            or now - last_sync >= self.min_requote_seconds
        )
        created = 0
        canceled = 0

        for order in tuple(self.orders.values()):
            if order.market_ticker != market_ticker:
                continue

            intent = wanted.get(order.quote_id)

            if intent is None or not order.matches(intent):
                await self._cancel(order)
                canceled += 1

        if not can_create:
            return 0, canceled

        live_quote_ids = {
            order.quote_id
            for order in self.orders.values()
            if order.market_ticker == market_ticker
        }
        create_intents = [
            intent
            for intent in wanted.values()
            if (
                intent.quote_id not in live_quote_ids
                and not self._is_rejected_quote_cooling_down(intent.quote_id, now)
            )
        ]

        if create_intents:
            self._last_sync_by_ticker[market_ticker] = now
            created += await self._create(create_intents, now=now)

        return created, canceled

    async def cancel_all(self) -> int:
        canceled = 0
        errors: list[str] = []

        for order in tuple(self.orders.values()):
            try:
                await self._cancel(order)
                canceled += 1
            except Exception as error:
                errors.append(f"{order.order_id}: {error}")

        if errors:
            raise RuntimeError("failed to cancel one or more live orders: " + "; ".join(errors))

        return canceled

    def handle_user_order(self, raw_msg: dict[str, Any]) -> None:
        if raw_msg.get("type") != "user_order":
            return

        data = raw_msg.get("msg")

        if not isinstance(data, dict):
            return

        client_order_id = str(data.get("client_order_id", ""))
        order_id = data.get("order_id")

        if not client_order_id.startswith(self.client_prefix + "-") or not order_id:
            return

        status = str(data.get("status", "")).lower()

        if status in TERMINAL_ORDER_STATUSES:
            self.orders.pop(client_order_id, None)
            return

        order = self.orders.get(client_order_id)

        if order is None:
            return

        remaining_count = _optional_count(data, "remaining_count_fp", "remaining_count")

        if remaining_count is None:
            return

        if remaining_count <= 0:
            self.orders.pop(client_order_id, None)
            return

        order.remaining_count = remaining_count

    async def _create(self, intents: list[QuoteIntent], *, now: float) -> int:
        client_ids = [self._next_client_order_id() for _ in intents]
        requests = [
            CreateOrderRequest(
                ticker=intent.market_ticker,
                side=order_book_side(intent.action, intent.side),
                price=intent.yes_price,
                count=intent.count,
                client_order_id=client_order_id,
            )
            for intent, client_order_id in zip(intents, client_ids, strict=True)
        ]

        if self.dry_run:
            for intent, client_order_id in zip(intents, client_ids, strict=True):
                self.orders[client_order_id] = LiveOrder.from_intent(
                    order_id=f"dry-{client_order_id}",
                    client_order_id=client_order_id,
                    intent=intent,
                )
                self._emit(
                    f"DRY create {intent.market_ticker} {intent.action} "
                    f"{format_count_fp(intent.count)} @ {format_price_fp(intent.yes_price)}"
                )

            return len(requests)

        available_balance_cents = await self.rest.get_available_balance_cents()
        required_balance_cents = sum(_estimated_required_cents(intent) for intent in intents)

        if available_balance_cents <= 0 or required_balance_cents > available_balance_cents:
            self._emit(
                f"Skipping {len(requests)} live order(s): available balance "
                f"{_format_cents(available_balance_cents)} below estimated requirement "
                f"{_format_cents(required_balance_cents)}"
            )
            return 0

        data = await self.rest.batch_create_orders(requests)
        raw_orders = data.get("orders")

        if not isinstance(raw_orders, list):
            raise TypeError("expected batch create orders list response")

        if len(raw_orders) != len(requests):
            raise RuntimeError(
                f"batch create returned {len(raw_orders)} order(s) for {len(requests)} request(s)"
            )

        ordered_raw_orders = _ordered_create_response(raw_orders, client_ids)

        created = 0

        for intent, client_order_id, raw_order in zip(
            intents,
            client_ids,
            ordered_raw_orders,
            strict=True,
        ):
            order_id = str(raw_order.get("order_id", ""))

            if not order_id:
                self._rejected_quote_until[intent.quote_id] = (
                    now + self.rejection_cooldown_seconds
                )
                self._emit(
                    f"Rejected live order {client_order_id} "
                    f"{intent.action} {format_count_fp(intent.count)} "
                    f"@ {format_price_fp(intent.yes_price)}: "
                    f"{_create_response_error_summary(raw_order)}"
                )
                continue

            remaining_count = _optional_count(raw_order, "remaining_count", "remaining_count_fp")

            if remaining_count is not None and remaining_count <= 0:
                self._rejected_quote_until.pop(intent.quote_id, None)
                created += 1
                continue

            self._rejected_quote_until.pop(intent.quote_id, None)
            self.orders[client_order_id] = LiveOrder.from_intent(
                order_id=order_id,
                client_order_id=client_order_id,
                intent=intent,
                remaining_count=remaining_count,
            )
            created += 1

        self._emit(f"Created {created}/{len(requests)} live order(s)")
        return created

    async def _cancel(self, order: LiveOrder) -> None:
        await self._cancel_order_id(order.order_id, reason="quote replaced")
        self.orders.pop(order.client_order_id, None)

    async def _cancel_order_id(self, order_id: str, *, reason: str) -> None:
        if self.dry_run:
            self._emit(f"DRY cancel {order_id} ({reason})")
            self._recently_canceled_order_ids.add(order_id)
            return

        await self.rest.batch_cancel_orders([CancelOrderRequest(order_id=order_id)])
        self._recently_canceled_order_ids.add(order_id)
        self._emit(f"Canceled {order_id} ({reason})")

    def _next_client_order_id(self) -> str:
        client_order_id = f"{self.client_prefix}-{self._run_id}-{self._next_order_number}"
        self._next_order_number += 1
        return client_order_id

    def _is_rejected_quote_cooling_down(self, quote_id: str, now: float) -> bool:
        rejected_until = self._rejected_quote_until.get(quote_id)

        if rejected_until is None:
            return False

        if now >= rejected_until:
            self._rejected_quote_until.pop(quote_id, None)
            return False

        return True

    def _emit(self, message: str) -> None:
        if self.status is not None:
            self.status(message)


async def run_live_strategy(
    *,
    tickers: tuple[str, ...],
    strategy: Strategy,
    prod: bool = False,
    dry_run: bool = True,
    duration_seconds: float | None = None,
    client_prefix: str = "kmm",
    min_requote_seconds: float = 0.0,
    cancel_on_stop: bool = True,
    status: StatusCallback | None = None,
    stop_requested: StopRequested | None = None,
) -> LiveRunStats:
    if not tickers:
        raise ValueError("at least one ticker is required")

    settings = load_settings()
    environment = settings.environment(prod=prod)
    auth = KalshiAuth(settings.api_key_id, settings.private_key_path)
    rest = KalshiRestClient(environment.rest_base_url, auth)
    controller = FeedController(
        rest=rest,
        ws=KalshiWebSocketClient(environment.ws_url, auth),
    )
    order_manager = LiveOrderManager(
        rest,
        dry_run=dry_run,
        client_prefix=client_prefix,
        min_requote_seconds=min_requote_seconds,
        status=status,
    )
    stats = LiveRunStats(dry_run=dry_run)
    started = time.monotonic()

    try:
        _emit(status, f"Connecting to {environment.name} ({'dry-run' if dry_run else 'LIVE'})")
        portfolio = await LivePortfolio.load(rest, tickers)
        await controller.connect()

        if not dry_run:
            canceled = await order_manager.cancel_stale_bot_orders(tickers)
            stats.cancel_count += canceled

        await controller.subscribe(
            tickers,
            channels=(
                ORDERBOOK_CHANNEL,
                FILL_CHANNEL,
                MARKET_POSITIONS_CHANNEL,
                USER_ORDERS_CHANNEL,
            ),
        )
        _emit(status, f"Subscribed to {', '.join(tickers)}")

        while True:
            if stop_requested is not None and stop_requested():
                break

            if duration_seconds is not None and time.monotonic() - started >= duration_seconds:
                break

            try:
                update = await controller.recv_update(timeout=0.5)
            except TimeoutError:
                continue

            stats.event_count += 1
            portfolio.apply_message(update.parsed)

            if isinstance(update.parsed, OrderFill):
                stats.fill_count += 1

            order_manager.handle_user_order(update.raw_msg)

            if update.updated_ticker is None:
                continue

            stats.orderbook_updates += 1
            book = controller.orderbooks.get(update.updated_ticker)

            if book is None:
                continue

            context = StrategyContext(
                event_count=stats.event_count,
                offset_seconds=time.monotonic() - started,
                observed_at_utc=utc_now_iso(),
            )
            intents = strategy.on_orderbook(context, update.updated_ticker, book, portfolio)
            created, canceled = await order_manager.sync_quotes(update.updated_ticker, intents)
            stats.create_count += created
            stats.cancel_count += canceled
    finally:
        try:
            if cancel_on_stop:
                stats.cancel_count += await _cancel_on_shutdown(
                    order_manager,
                    tickers,
                    dry_run=dry_run,
                )
        finally:
            await controller.close()

    return stats


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _emit(status: StatusCallback | None, message: str) -> None:
    if status is not None:
        status(message)


async def _cancel_on_shutdown(
    order_manager: LiveOrderManager,
    tickers: tuple[str, ...],
    *,
    dry_run: bool,
) -> int:
    canceled = 0
    errors: list[str] = []

    try:
        canceled += await order_manager.cancel_all()
    except Exception as error:
        errors.append(str(error))

    if not dry_run:
        try:
            canceled += await order_manager.cancel_stale_bot_orders(
                tickers,
                reason="shutdown sweep",
            )
        except Exception as error:
            errors.append(str(error))

    if errors:
        raise RuntimeError("shutdown cancel failed: " + "; ".join(errors))

    return canceled


def _optional_count(data: dict[str, Any], *names: str) -> int | None:
    for name in names:
        raw_count = data.get(name)

        if raw_count is not None and raw_count != "":
            return parse_count_fp(str(raw_count))

    return None


def _ordered_create_response(
    raw_orders: list[Any],
    client_order_ids: list[str],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    has_response_client_ids = True

    for raw_order in raw_orders:
        if not isinstance(raw_order, dict):
            raise TypeError("expected batch create order entry")

        entries.append(raw_order)

        response_client_id = raw_order.get("client_order_id")

        if response_client_id is None or response_client_id == "":
            has_response_client_ids = False

    if not has_response_client_ids:
        return entries

    entries_by_client_id: dict[str, dict[str, Any]] = {}

    for entry in entries:
        response_client_id = str(entry["client_order_id"])

        if response_client_id in entries_by_client_id:
            raise RuntimeError(f"duplicate create response client_order_id: {response_client_id}")

        entries_by_client_id[response_client_id] = entry

    expected = set(client_order_ids)
    actual = set(entries_by_client_id)

    if expected != actual:
        missing = ", ".join(sorted(expected - actual))
        unexpected = ", ".join(sorted(actual - expected))
        raise RuntimeError(
            "batch create response client_order_id mismatch "
            f"(missing={missing!r}, unexpected={unexpected!r})"
        )

    return [entries_by_client_id[client_order_id] for client_order_id in client_order_ids]


def _create_response_error_summary(raw_order: dict[str, Any]) -> str:
    error = raw_order.get("error")

    if isinstance(error, dict):
        summary = _error_fields(error)
    elif error is not None:
        summary = str(error)
    else:
        summary = _error_fields(raw_order)

    if summary:
        return summary

    return repr(raw_order)[:300]


def _error_fields(data: dict[str, Any]) -> str:
    parts = [
        str(data[key])
        for key in ("code", "message", "details", "reason", "status")
        if data.get(key) not in {None, ""}
    ]
    return "; ".join(parts)


def _estimated_required_cents(intent: QuoteIntent) -> int:
    risk_price = intent.yes_price if intent.action == "buy" else ONE_DOLLAR - intent.yes_price
    return _ceil_div(risk_price * intent.count * 100, PRICE_SCALE * COUNT_SCALE)


def _format_cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100}.{cents % 100:02d}"


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)
