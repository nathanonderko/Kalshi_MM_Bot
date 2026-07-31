import asyncio

import pytest

from kalshi_mm_bot.api.rest import CancelOrderRequest, CreateOrderRequest, KalshiRestClient
from kalshi_mm_bot.market.price import COUNT_SCALE, parse_price_fp


class PagingRestClient(KalshiRestClient):
    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.calls: list[dict] = []

    async def _request(self, _method: str, _path: str, *, params: dict, **_) -> dict:
        self.calls.append(params)
        return self.pages[len(self.calls) - 1]


class StaticRestClient(KalshiRestClient):
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    async def _request(self, method: str, path: str, **_) -> dict:
        self.calls.append((method, path))
        return self.response


def test_create_order_request_uses_v2_event_order_payload() -> None:
    payload = CreateOrderRequest(
        ticker="M1",
        side="bid",
        price=parse_price_fp("0.4200"),
        count=2 * COUNT_SCALE,
        client_order_id="kmm-test-1",
    ).to_json()

    assert payload == {
        "ticker": "M1",
        "side": "bid",
        "count": "2.00",
        "price": "0.4200",
        "client_order_id": "kmm-test-1",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": True,
        "reduce_only": False,
        "cancel_order_on_pause": True,
        "exchange_index": 0,
    }


def test_cancel_order_request_uses_v2_batch_cancel_payload() -> None:
    assert CancelOrderRequest("order-1").to_json() == {
        "order_id": "order-1",
        "exchange_index": 0,
    }


def test_get_available_balance_cents_reads_portfolio_balance() -> None:
    async def run() -> None:
        rest = StaticRestClient({"balance": 12345})

        assert await rest.get_available_balance_cents() == 12345
        assert rest.calls == [("GET", "/portfolio/balance")]

    asyncio.run(run())


def test_get_orders_reads_all_cursor_pages() -> None:
    async def run() -> None:
        rest = PagingRestClient(
            [
                {"orders": [{"order_id": "order-1"}], "cursor": "next"},
                {"orders": [{"order_id": "order-2"}]},
            ]
        )

        orders = await rest.get_orders(ticker="M1", status="resting", limit=1)

        assert orders == [{"order_id": "order-1"}, {"order_id": "order-2"}]
        assert rest.calls == [
            {"limit": 1, "ticker": "M1", "status": "resting"},
            {"limit": 1, "ticker": "M1", "status": "resting", "cursor": "next"},
        ]

    asyncio.run(run())


def test_get_orders_rejects_repeated_cursor() -> None:
    async def run() -> None:
        rest = PagingRestClient(
            [
                {"orders": [], "cursor": "same"},
                {"orders": [], "cursor": "same"},
            ]
        )

        with pytest.raises(RuntimeError, match="repeated orders cursor"):
            await rest.get_orders()

    asyncio.run(run())
