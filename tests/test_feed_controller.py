import asyncio
from collections import deque

from kalshi_mm_bot.api.feed_controller import (
    FEED_CHANNELS,
    FeedController,
    ORDERBOOK_CHANNEL,
)
from kalshi_mm_bot.market.types import PriceRange


class FakeRest:
    def __init__(self) -> None:
        self.requests: list[list[str]] = []
        self.closed = False

    async def get_market_price_ranges(
        self,
        tickers: list[str] | tuple[str, ...],
    ) -> dict[str, tuple[PriceRange, ...]]:
        self.requests.append(list(tickers))
        return {
            ticker: (PriceRange(start=0, end=10000, step=10),)
            for ticker in tickers
        }

    async def close(self) -> None:
        self.closed = True


class FakeWs:
    def __init__(self, incoming: list[dict]) -> None:
        self.incoming = deque(incoming)
        self.sent: list[dict] = []
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def recv_json(self) -> dict:
        return self.incoming.popleft()


def subscribed(command_id: int, channel: str, sid: int) -> dict:
    return {
        "id": command_id,
        "type": "subscribed",
        "msg": {"channel": channel, "sid": sid},
    }


def ok(command_id: int, sid: int, seq: int = 1) -> dict:
    return {
        "id": command_id,
        "sid": sid,
        "seq": seq,
        "type": "ok",
        "msg": {"market_tickers": []},
    }


def unsubscribed(command_id: int, sid: int, seq: int = 1) -> dict:
    return {"id": command_id, "sid": sid, "seq": seq, "type": "unsubscribed"}


def snapshot(seq: int, ticker: str, bid: str = "0.5000") -> dict:
    return {
        "type": "orderbook_snapshot",
        "sid": 10,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": [[bid, "1.00"]],
            "no_dollars_fp": [],
        },
    }


def delta(seq: int, ticker: str, price: str, amount: str) -> dict:
    return {
        "type": "orderbook_delta",
        "sid": 10,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "side": "yes",
            "price_dollars": price,
            "delta_fp": amount,
        },
    }


def test_subscribe_all_channels_tracks_one_sid_per_channel() -> None:
    rest = FakeRest()
    ws = FakeWs(
        [
            subscribed(1, "orderbook_delta", 10),
            subscribed(2, "fill", 11),
            subscribed(3, "market_positions", 12),
        ]
    )
    controller = FeedController(rest=rest, ws=ws)

    asyncio.run(controller.subscribe(["M1", "M2"], channels=FEED_CHANNELS))

    assert controller.sids == {
        "orderbook_delta": 10,
        "fill": 11,
        "market_positions": 12,
    }
    assert controller.subscribed_markets["orderbook_delta"] == frozenset({"M1", "M2"})
    assert controller.subscribed_markets["fill"] == frozenset({"M1", "M2"})
    assert controller.subscribed_markets["market_positions"] == frozenset({"M1", "M2"})
    assert rest.requests == [["M1", "M2"]]
    assert [payload["cmd"] for payload in ws.sent] == ["subscribe", "subscribe", "subscribe"]
    assert ws.sent[0]["params"] == {
        "channels": ["orderbook_delta"],
        "market_tickers": ["M1", "M2"],
        "use_yes_price": True,
    }


def test_recv_builds_and_updates_orderbooks_for_multiple_markets() -> None:
    rest = FakeRest()
    ws = FakeWs(
        [
            subscribed(1, "orderbook_delta", 10),
            snapshot(2, "M1", "0.5000"),
            snapshot(3, "M2", "0.3000"),
            delta(4, "M1", "0.6000", "2.00"),
            delta(5, "M2", "0.4000", "3.00"),
        ]
    )
    controller = FeedController(rest=rest, ws=ws)

    asyncio.run(controller.subscribe(["M1", "M2"], channels=(ORDERBOOK_CHANNEL,)))

    assert asyncio.run(controller.recv()) == "M1"
    assert asyncio.run(controller.recv()) == "M2"
    assert asyncio.run(controller.recv()) == "M1"
    assert asyncio.run(controller.recv()) == "M2"
    assert controller.orderbooks["M1"].best_bid == 6000
    assert controller.orderbooks["M1"].bids[6000] == 200
    assert controller.orderbooks["M2"].best_bid == 4000
    assert controller.orderbooks["M2"].bids[4000] == 300


def test_subscribe_more_markets_uses_update_subscription_when_sid_exists() -> None:
    rest = FakeRest()
    ws = FakeWs(
        [
            subscribed(1, "orderbook_delta", 10),
            ok(2, 10, seq=1),
        ]
    )
    controller = FeedController(rest=rest, ws=ws)

    asyncio.run(controller.subscribe(["M1"], channels=(ORDERBOOK_CHANNEL,)))
    asyncio.run(controller.subscribe(["M2"], channels=(ORDERBOOK_CHANNEL,)))

    assert ws.sent[1] == {
        "id": 2,
        "cmd": "update_subscription",
        "params": {
            "sids": [10],
            "market_tickers": ["M2"],
            "action": "add_markets",
        },
    }
    assert controller.subscribed_markets["orderbook_delta"] == frozenset({"M1", "M2"})
    assert rest.requests == [["M1"], ["M2"]]


def test_unsubscribe_partial_market_uses_delete_markets_and_drops_book() -> None:
    rest = FakeRest()
    ws = FakeWs(
        [
            subscribed(1, "orderbook_delta", 10),
            snapshot(2, "M1"),
            snapshot(3, "M2"),
            ok(2, 10, seq=4),
        ]
    )
    controller = FeedController(rest=rest, ws=ws)

    asyncio.run(controller.subscribe(["M1", "M2"], channels=(ORDERBOOK_CHANNEL,)))
    asyncio.run(controller.recv())
    asyncio.run(controller.recv())
    asyncio.run(controller.unsubscribe(["M1"], channels=(ORDERBOOK_CHANNEL,)))

    assert ws.sent[1] == {
        "id": 2,
        "cmd": "update_subscription",
        "params": {
            "sids": [10],
            "market_tickers": ["M1"],
            "action": "delete_markets",
        },
    }
    assert "M1" not in controller.orderbooks
    assert "M2" in controller.orderbooks
    assert controller.subscribed_markets["orderbook_delta"] == frozenset({"M2"})


def test_unsubscribe_last_market_clears_channel_sid() -> None:
    rest = FakeRest()
    ws = FakeWs(
        [
            subscribed(1, "orderbook_delta", 10),
            snapshot(2, "M1"),
            unsubscribed(2, 10, seq=3),
        ]
    )
    controller = FeedController(rest=rest, ws=ws)

    asyncio.run(controller.subscribe(["M1"], channels=(ORDERBOOK_CHANNEL,)))
    asyncio.run(controller.recv())
    asyncio.run(controller.unsubscribe(["M1"], channels=(ORDERBOOK_CHANNEL,)))

    assert ws.sent[1] == {"id": 2, "cmd": "unsubscribe", "params": {"sids": [10]}}
    assert controller.sids == {}
    assert controller.subscribed_markets["orderbook_delta"] == frozenset()
    assert controller.orderbooks == {}


def test_unsubscribe_last_market_clears_channel_on_ok_ack() -> None:
    rest = FakeRest()
    ws = FakeWs(
        [
            subscribed(1, "orderbook_delta", 10),
            snapshot(2, "M1"),
            ok(2, 10, seq=3),
        ]
    )
    controller = FeedController(rest=rest, ws=ws)

    asyncio.run(controller.subscribe(["M1"], channels=(ORDERBOOK_CHANNEL,)))
    asyncio.run(controller.recv())
    asyncio.run(controller.unsubscribe(["M1"], channels=(ORDERBOOK_CHANNEL,)))

    assert controller.sids == {}
    assert controller.subscribed_markets["orderbook_delta"] == frozenset()
    assert controller.orderbooks == {}


def test_unsubscribe_channels_sends_one_command_per_sid() -> None:
    rest = FakeRest()
    ws = FakeWs(
        [
            subscribed(1, "orderbook_delta", 10),
            subscribed(2, "fill", 11),
            subscribed(3, "market_positions", 12),
            unsubscribed(4, 10, seq=1),
            unsubscribed(5, 11, seq=1),
            unsubscribed(6, 12, seq=1),
        ]
    )
    controller = FeedController(rest=rest, ws=ws)

    asyncio.run(controller.subscribe(["M1"], channels=FEED_CHANNELS))
    asyncio.run(controller.unsubscribe_channels(FEED_CHANNELS))

    assert ws.sent[3:] == [
        {"id": 4, "cmd": "unsubscribe", "params": {"sids": [10]}},
        {"id": 5, "cmd": "unsubscribe", "params": {"sids": [11]}},
        {"id": 6, "cmd": "unsubscribe", "params": {"sids": [12]}},
    ]
    assert controller.sids == {}


def test_close_resets_feed_state() -> None:
    rest = FakeRest()
    ws = FakeWs([subscribed(1, "orderbook_delta", 10), snapshot(2, "M1")])
    controller = FeedController(rest=rest, ws=ws)

    asyncio.run(controller.subscribe(["M1"], channels=(ORDERBOOK_CHANNEL,)))
    asyncio.run(controller.recv())
    asyncio.run(controller.close())

    assert ws.closed
    assert rest.closed
    assert controller.sids == {}
    assert controller.orderbooks == {}
    assert controller.subscribed_markets["orderbook_delta"] == frozenset()
