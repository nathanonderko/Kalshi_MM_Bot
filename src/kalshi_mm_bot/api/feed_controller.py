from __future__ import annotations

from asyncio import Future, TimeoutError, get_running_loop, wait_for
from collections.abc import Iterable
from typing import Any, Literal, cast

from kalshi_mm_bot.api.rest import KalshiRestClient
from kalshi_mm_bot.api.websocket import KalshiWebSocketClient
from kalshi_mm_bot.market.orderbook import Orderbook, SequenceGapError
from kalshi_mm_bot.market.price import parse_count_fp, parse_price_fp
from kalshi_mm_bot.market.types import BookSide, PriceRange

FeedChannel = Literal["orderbook_delta", "fill", "market_positions"]
SubscriptionAction = Literal["add_markets", "delete_markets"]

ORDERBOOK_CHANNEL: FeedChannel = "orderbook_delta"
FILL_CHANNEL: FeedChannel = "fill"
MARKET_POSITIONS_CHANNEL: FeedChannel = "market_positions"

FEED_CHANNELS: tuple[FeedChannel, ...] = (
    ORDERBOOK_CHANNEL,
    FILL_CHANNEL,
    MARKET_POSITIONS_CHANNEL,
)
DEFAULT_FEED_CHANNELS: tuple[FeedChannel, ...] = (ORDERBOOK_CHANNEL,)


class FeedControllerError(Exception):
    pass


class FeedController:
    def __init__(
        self,
        rest: KalshiRestClient,
        ws: KalshiWebSocketClient,
        command_timeout: float = 10.0,
    ) -> None:
        self.rest = rest
        self.ws = ws
        self.command_timeout = command_timeout

        self.price_ranges_by_ticker: dict[str, tuple[PriceRange, ...]] = {}
        self.orderbooks: dict[str, Orderbook] = {}

        self._command_id = 1
        self._command_waiters: dict[int, Future[dict[str, Any]]] = {}
        self._receiver_running = False
        self._sids: dict[FeedChannel, int] = {}
        self._markets_by_channel: dict[FeedChannel, set[str]] = {
            channel: set() for channel in FEED_CHANNELS
        }
        self._last_seq_by_sid: dict[int, int] = {}

    @property
    def sids(self) -> dict[FeedChannel, int]:
        return dict(self._sids)

    @property
    def subscribed_markets(self) -> dict[FeedChannel, frozenset[str]]:
        return {
            channel: frozenset(markets)
            for channel, markets in self._markets_by_channel.items()
        }

    async def connect(self) -> None:
        await self.ws.connect()

    async def close(self) -> None:
        await self.ws.close()
        await self.rest.close()
        self._reset_feed_state()

    async def __aenter__(self) -> "FeedController":
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def subscribe(
        self,
        tickers: Iterable[str],
        channels: Iterable[FeedChannel] = DEFAULT_FEED_CHANNELS,
    ) -> None:
        ticker_tuple = _unique_tuple(tickers)

        if not ticker_tuple:
            return

        channel_tuple = _channel_tuple(channels)

        if ORDERBOOK_CHANNEL in channel_tuple:
            await self._load_price_ranges(ticker_tuple)

        for channel in channel_tuple:
            await self._subscribe_markets(channel, ticker_tuple)

    async def unsubscribe(
        self,
        tickers: Iterable[str],
        channels: Iterable[FeedChannel] = DEFAULT_FEED_CHANNELS,
    ) -> None:
        ticker_tuple = _unique_tuple(tickers)

        if not ticker_tuple:
            return

        for channel in _channel_tuple(channels):
            await self._unsubscribe_markets(channel, ticker_tuple)

    async def unsubscribe_channels(
        self,
        channels: Iterable[FeedChannel] = FEED_CHANNELS,
    ) -> None:
        for channel in _channel_tuple(channels):
            sid = self._sids.get(channel)

            if sid is None:
                continue

            await self._run_command("unsubscribe", {"sids": [sid]})
            if self._sids.get(channel) == sid:
                self._clear_channel(channel)

    async def recv(self) -> str | None:
        return await self._recv_and_dispatch()

    async def run_forever(self) -> None:
        if self._receiver_running:
            raise FeedControllerError("feed receiver is already running")

        self._receiver_running = True

        try:
            while True:
                await self._recv_and_dispatch()
        finally:
            self._receiver_running = False

    def handle_message(self, raw_msg: dict[str, Any]) -> str | None:
        self._update_sid_sequence(raw_msg)
        msg_type = raw_msg.get("type")

        if msg_type == "orderbook_delta":
            return self._handle_orderbook_delta(raw_msg)

        if msg_type == "orderbook_snapshot":
            return self._handle_orderbook_snapshot(raw_msg)

        if msg_type in {"subscribed", "unsubscribed", "ok", "error"}:
            self._handle_control_message(raw_msg)

        return None

    async def _subscribe_markets(
        self,
        channel: FeedChannel,
        tickers: tuple[str, ...],
    ) -> None:
        current = self._markets_by_channel[channel]
        new_tickers = tuple(ticker for ticker in tickers if ticker not in current)

        if not new_tickers:
            return

        current.update(new_tickers)

        try:
            sid = self._sids.get(channel)

            if sid is None:
                params: dict[str, Any] = {
                    "channels": [channel],
                    "market_tickers": list(new_tickers),
                }

                if channel == ORDERBOOK_CHANNEL:
                    params["use_yes_price"] = True

                await self._run_command("subscribe", params)

                if channel not in self._sids:
                    raise FeedControllerError(f"missing sid for channel {channel!r}")

                return

            await self._update_subscription(sid, new_tickers, "add_markets")
        except Exception:
            current.difference_update(new_tickers)

            if channel == ORDERBOOK_CHANNEL:
                for ticker in new_tickers:
                    self.orderbooks.pop(ticker, None)

            raise

    async def _unsubscribe_markets(
        self,
        channel: FeedChannel,
        tickers: tuple[str, ...],
    ) -> None:
        current = self._markets_by_channel[channel]
        removed_tickers = tuple(ticker for ticker in tickers if ticker in current)

        if not removed_tickers:
            return

        sid = self._sids.get(channel)

        if sid is None:
            raise FeedControllerError(f"missing sid for channel {channel!r}")

        if len(removed_tickers) == len(current):
            await self._run_command("unsubscribe", {"sids": [sid]})
            if self._sids.get(channel) == sid:
                self._clear_channel(channel)
            return

        await self._update_subscription(sid, removed_tickers, "delete_markets")
        current.difference_update(removed_tickers)
        self._drop_orderbooks(channel, removed_tickers)

    async def _update_subscription(
        self,
        sid: int,
        tickers: tuple[str, ...],
        action: SubscriptionAction,
    ) -> None:
        await self._run_command(
            "update_subscription",
            {"sids": [sid], "market_tickers": list(tickers), "action": action},
        )

    async def _load_price_ranges(self, tickers: tuple[str, ...]) -> None:
        missing = [ticker for ticker in tickers if ticker not in self.price_ranges_by_ticker]

        if not missing:
            return

        self.price_ranges_by_ticker.update(await self.rest.get_market_price_ranges(missing))
        not_found = [ticker for ticker in missing if ticker not in self.price_ranges_by_ticker]

        if not_found:
            raise FeedControllerError(f"missing price ranges for: {', '.join(not_found)}")

    async def _run_command(
        self,
        cmd: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        loop = get_running_loop()
        command_id = self._command_id
        self._command_id += 1
        waiter: Future[dict[str, Any]] = loop.create_future()
        self._command_waiters[command_id] = waiter

        payload: dict[str, Any] = {"id": command_id, "cmd": cmd}

        if params is not None:
            payload["params"] = params

        try:
            await self.ws.send_json(payload)
            raw_msg = await self._wait_for_command(command_id, waiter)
        finally:
            self._command_waiters.pop(command_id, None)

        if raw_msg.get("type") == "error":
            raise FeedControllerError(f"Kalshi WS error: {raw_msg.get('msg')}")

    async def _wait_for_command(
        self,
        command_id: int,
        waiter: Future[dict[str, Any]],
    ) -> dict[str, Any]:
        if self._receiver_running:
            try:
                return await wait_for(waiter, timeout=self.command_timeout)
            except TimeoutError as error:
                raise FeedControllerError(
                    f"timed out waiting for command {command_id}"
                ) from error

        loop = get_running_loop()
        deadline = loop.time() + self.command_timeout

        while not waiter.done():
            remaining = deadline - loop.time()

            if remaining <= 0:
                raise FeedControllerError(f"timed out waiting for command {command_id}")

            try:
                await self._recv_and_dispatch(timeout=remaining)
            except TimeoutError as error:
                raise FeedControllerError(
                    f"timed out waiting for command {command_id}"
                ) from error

        return waiter.result()

    async def _recv_and_dispatch(self, timeout: float | None = None) -> str | None:
        if timeout is None:
            raw_msg = await self.ws.recv_json()
        else:
            raw_msg = await wait_for(self.ws.recv_json(), timeout=timeout)

        return self.handle_message(raw_msg)

    def _handle_control_message(self, raw_msg: dict[str, Any]) -> None:
        msg_type = raw_msg.get("type")
        command_id = raw_msg.get("id")
        command_waiter = (
            self._command_waiters.get(command_id)
            if isinstance(command_id, int)
            else None
        )

        if msg_type == "error":
            if command_waiter is not None and not command_waiter.done():
                command_waiter.set_result(raw_msg)
                return

            raise FeedControllerError(f"Kalshi WS error: {raw_msg.get('msg')}")

        if msg_type == "subscribed":
            msg = raw_msg["msg"]
            channel = cast(FeedChannel, msg["channel"])

            if channel in FEED_CHANNELS:
                self._sids[channel] = msg["sid"]

        elif msg_type == "unsubscribed":
            sid = raw_msg.get("sid")

            for channel, channel_sid in tuple(self._sids.items()):
                if channel_sid == sid:
                    self._clear_channel(channel)
                    break

        if command_waiter is not None and not command_waiter.done():
            command_waiter.set_result(raw_msg)

    def _handle_orderbook_snapshot(self, raw_msg: dict[str, Any]) -> str | None:
        if raw_msg.get("sid") != self._sids.get(ORDERBOOK_CHANNEL):
            return None

        data = raw_msg["msg"]
        ticker = data["market_ticker"]

        if ticker not in self._markets_by_channel[ORDERBOOK_CHANNEL]:
            return None

        price_ranges = self.price_ranges_by_ticker.get(ticker)

        if price_ranges is None:
            raise FeedControllerError(f"missing price ranges for orderbook: {ticker}")

        self.orderbooks[ticker] = Orderbook.from_snapshot(
            market_ticker=ticker,
            seq=raw_msg["seq"],
            bids_raw=data.get("yes_dollars_fp", ()),
            asks_raw=data.get("no_dollars_fp", ()),
            price_ranges=price_ranges,
        )
        return ticker

    def _handle_orderbook_delta(self, raw_msg: dict[str, Any]) -> str | None:
        if raw_msg.get("sid") != self._sids.get(ORDERBOOK_CHANNEL):
            return None

        data = raw_msg["msg"]
        ticker = data["market_ticker"]

        if ticker not in self._markets_by_channel[ORDERBOOK_CHANNEL]:
            return None

        orderbook = self.orderbooks.get(ticker)

        if orderbook is None:
            raise FeedControllerError(f"received orderbook delta before snapshot: {ticker}")

        orderbook.apply_delta(
            seq=raw_msg["seq"],
            side=_book_side(data["side"]),
            price=parse_price_fp(data["price_dollars"]),
            delta=parse_count_fp(data["delta_fp"]),
        )
        return ticker

    def _update_sid_sequence(self, raw_msg: dict[str, Any]) -> None:
        sid = raw_msg.get("sid")
        seq = raw_msg.get("seq")

        if not isinstance(sid, int) or not isinstance(seq, int):
            return

        if sid not in self._sids.values():
            return

        last_seq = self._last_seq_by_sid.get(sid)

        if last_seq is not None and seq != last_seq + 1:
            raise SequenceGapError(f"sid {sid}: expected seq {last_seq + 1}, got {seq}")

        self._last_seq_by_sid[sid] = seq

    def _clear_channel(self, channel: FeedChannel) -> None:
        tickers = tuple(self._markets_by_channel[channel])
        sid = self._sids.pop(channel, None)

        if sid is not None:
            self._last_seq_by_sid.pop(sid, None)

        self._markets_by_channel[channel].clear()
        self._drop_orderbooks(channel, tickers)

    def _drop_orderbooks(self, channel: FeedChannel, tickers: tuple[str, ...]) -> None:
        if channel != ORDERBOOK_CHANNEL:
            return

        for ticker in tickers:
            self.orderbooks.pop(ticker, None)

    def _reset_feed_state(self) -> None:
        for waiter in self._command_waiters.values():
            if not waiter.done():
                waiter.cancel()

        self._command_waiters.clear()
        self._sids.clear()
        self._last_seq_by_sid.clear()
        self.orderbooks.clear()

        for markets in self._markets_by_channel.values():
            markets.clear()


def _unique_tuple(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _channel_tuple(channels: Iterable[FeedChannel]) -> tuple[FeedChannel, ...]:
    channel_tuple = tuple(dict.fromkeys(channels))

    for channel in channel_tuple:
        if channel not in FEED_CHANNELS:
            raise ValueError(f"unsupported feed channel: {channel!r}")

    return channel_tuple


def _book_side(outcome_side: str) -> BookSide:
    if outcome_side == "yes":
        return "bid"

    if outcome_side == "no":
        return "ask"

    raise ValueError(f"unknown orderbook side: {outcome_side!r}")
