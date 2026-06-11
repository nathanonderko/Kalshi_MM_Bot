from dataclasses import dataclass
from typing import Any

import httpx

from kalshi_mm_bot.api.auth import KalshiAuth
from kalshi_mm_bot.api.parser import parse_price_ranges
from kalshi_mm_bot.market.price import format_count_fp, format_price_fp
from kalshi_mm_bot.market.types import BookSide, PriceRange


@dataclass(frozen=True, slots=True)
class CreateOrderRequest:
    ticker: str
    side: BookSide
    price: int
    count: int

    def to_json(self) -> dict[str, Any]:
        return _order_payload(
            ticker=self.ticker,
            side=self.side,
            price=self.price,
            count=self.count,
            post_only=True,
            reduce_only=False,
            cancel_order_on_pause=True,
        )


@dataclass(frozen=True, slots=True)
class AmendOrderRequest:
    order_id: str
    ticker: str
    side: BookSide
    price: int
    count: int

    def to_json(self) -> dict[str, Any]:
        return _order_payload(
            ticker=self.ticker,
            side=self.side,
            price=self.price,
            count=self.count,
        )


@dataclass(frozen=True, slots=True)
class CancelOrderRequest:
    order_id: str

    def to_json(self) -> dict[str, Any]:
        return {"order_id": self.order_id}


class KalshiRestClient:
    def __init__(
        self,
        base_url: str,
        auth: KalshiAuth,
        api_path_prefix: str = "/trade-api/v2",
    ) -> None:
        self.base_url = base_url
        self.auth = auth
        self.api_path_prefix = api_path_prefix
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=10.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "KalshiRestClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def get_market_price_ranges(
        self,
        tickers: list[str] | tuple[str, ...],
    ) -> dict[str, tuple[PriceRange, ...]]:
        if not tickers:
            return {}

        data = await self._request(
            "GET",
            "/markets",
            params={"tickers": ",".join(tickers)},
        )

        return {
            raw_market["ticker"]: parse_price_ranges(raw_market)
            for raw_market in data["markets"]
        }

    async def batch_create_orders(
        self,
        orders: list[CreateOrderRequest],
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/portfolio/orders/batched",
            json_body={"orders": [order.to_json() for order in orders]},
        )

    async def amend_order(
        self,
        request: AmendOrderRequest,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/portfolio/orders/{request.order_id}/amend",
            json_body=request.to_json(),
        )

    async def batch_cancel_orders(
        self,
        orders: list[CancelOrderRequest],
    ) -> dict[str, Any]:
        return await self._request(
            "DELETE",
            "/portfolio/orders/batched",
            json_body={"orders": [order.to_json() for order in orders]},
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        signed_path = f"{self.api_path_prefix}{path}"
        headers = self.auth.signed_headers(method, signed_path)

        if json_body is not None:
            headers["Content-Type"] = "application/json"

        response = await self._client.request(
            method=method,
            url=path,
            params=params,
            json=json_body,
            headers=headers,
        )
        response.raise_for_status()

        if not response.content:
            return {}

        data = response.json()

        if not isinstance(data, dict):
            raise TypeError("expected JSON object response")

        return data


def _order_payload(
    *,
    ticker: str,
    side: BookSide,
    price: int,
    count: int,
    post_only: bool | None = None,
    reduce_only: bool | None = None,
    cancel_order_on_pause: bool | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ticker": ticker,
        "side": "yes" if side == "bid" else "no",
        "count_fp": format_count_fp(count),
        "yes_price_dollars": format_price_fp(price),
        "action": "buy",
    }

    if post_only is not None:
        payload["post_only"] = post_only

    if reduce_only is not None:
        payload["reduce_only"] = reduce_only

    if cancel_order_on_pause is not None:
        payload["cancel_order_on_pause"] = cancel_order_on_pause

    return payload
