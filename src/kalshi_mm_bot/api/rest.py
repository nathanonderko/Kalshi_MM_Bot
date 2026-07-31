from dataclasses import dataclass
from typing import Any

import httpx

from kalshi_mm_bot.api.auth import KalshiAuth
from kalshi_mm_bot.api.parser import parse_price_ranges
from kalshi_mm_bot.market.price import format_count_fp, format_price_fp, parse_count_fp
from kalshi_mm_bot.market.types import BookSide, PriceRange


@dataclass(frozen=True, slots=True)
class CreateOrderRequest:
    ticker: str
    side: BookSide
    price: int
    count: int
    client_order_id: str | None = None
    time_in_force: str = "good_till_canceled"
    self_trade_prevention_type: str = "taker_at_cross"
    post_only: bool = True
    reduce_only: bool = False
    cancel_order_on_pause: bool = True
    exchange_index: int = 0

    def to_json(self) -> dict[str, Any]:
        return _order_payload(
            ticker=self.ticker,
            side=self.side,
            price=self.price,
            count=self.count,
            client_order_id=self.client_order_id,
            time_in_force=self.time_in_force,
            self_trade_prevention_type=self.self_trade_prevention_type,
            post_only=self.post_only,
            reduce_only=self.reduce_only,
            cancel_order_on_pause=self.cancel_order_on_pause,
            exchange_index=self.exchange_index,
        )


@dataclass(frozen=True, slots=True)
class AmendOrderRequest:
    order_id: str
    ticker: str
    side: BookSide
    price: int
    count: int
    client_order_id: str | None = None
    updated_client_order_id: str | None = None
    exchange_index: int = 0

    def to_json(self) -> dict[str, Any]:
        return _order_payload(
            ticker=self.ticker,
            side=self.side,
            price=self.price,
            count=self.count,
            client_order_id=self.client_order_id,
            updated_client_order_id=self.updated_client_order_id,
            exchange_index=self.exchange_index,
        )


@dataclass(frozen=True, slots=True)
class CancelOrderRequest:
    order_id: str
    subaccount: int | None = None
    exchange_index: int = 0

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "order_id": self.order_id,
            "exchange_index": self.exchange_index,
        }

        if self.subaccount is not None:
            payload["subaccount"] = self.subaccount

        return payload


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

    async def get_positions(
        self,
        tickers: list[str] | tuple[str, ...],
        *,
        subaccount: int | None = None,
    ) -> dict[str, int]:
        positions: dict[str, int] = {}

        for ticker in tickers:
            params: dict[str, Any] = {"ticker": ticker}

            if subaccount is not None:
                params["subaccount"] = subaccount

            data = await self._request("GET", "/portfolio/positions", params=params)

            for position in data.get("market_positions", ()):
                position_ticker = position.get("ticker", position.get("market_ticker"))

                if position_ticker == ticker:
                    positions[ticker] = parse_count_fp(position["position_fp"])

        return positions

    async def get_available_balance_cents(self) -> int:
        data = await self._request("GET", "/portfolio/balance")
        return int(data["balance"])

    async def get_orders(
        self,
        *,
        ticker: str | None = None,
        status: str | None = None,
        subaccount: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        base_params: dict[str, Any] = {"limit": limit}

        if ticker is not None:
            base_params["ticker"] = ticker

        if status is not None:
            base_params["status"] = status

        if subaccount is not None:
            base_params["subaccount"] = subaccount

        orders: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()

        while True:
            params = dict(base_params)

            if cursor is not None:
                params["cursor"] = cursor

            data = await self._request("GET", "/portfolio/orders", params=params)
            page_orders = data.get("orders", ())

            if not isinstance(page_orders, list):
                raise TypeError("expected orders list response")

            orders.extend(page_orders)
            cursor_value = data.get("cursor")

            if not cursor_value:
                return orders

            cursor = str(cursor_value)

            if cursor in seen_cursors:
                raise RuntimeError(f"repeated orders cursor: {cursor}")

            seen_cursors.add(cursor)

    async def batch_create_orders(
        self,
        orders: list[CreateOrderRequest],
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/portfolio/events/orders/batched",
            json_body={"orders": [order.to_json() for order in orders]},
        )

    async def amend_order(
        self,
        request: AmendOrderRequest,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/portfolio/events/orders/{request.order_id}/amend",
            json_body=request.to_json(),
        )

    async def batch_cancel_orders(
        self,
        orders: list[CancelOrderRequest],
    ) -> dict[str, Any]:
        return await self._request(
            "DELETE",
            "/portfolio/events/orders/batched",
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
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            detail = response.text[:500].replace("\n", " ")
            raise httpx.HTTPStatusError(
                f"{error} response_body={detail!r}",
                request=error.request,
                response=error.response,
            ) from error

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
    client_order_id: str | None = None,
    updated_client_order_id: str | None = None,
    time_in_force: str | None = None,
    self_trade_prevention_type: str | None = None,
    post_only: bool | None = None,
    reduce_only: bool | None = None,
    cancel_order_on_pause: bool | None = None,
    exchange_index: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ticker": ticker,
        "side": side,
        "count": format_count_fp(count),
        "price": format_price_fp(price),
    }

    if client_order_id is not None:
        payload["client_order_id"] = client_order_id

    if updated_client_order_id is not None:
        payload["updated_client_order_id"] = updated_client_order_id

    if time_in_force is not None:
        payload["time_in_force"] = time_in_force

    if self_trade_prevention_type is not None:
        payload["self_trade_prevention_type"] = self_trade_prevention_type

    if post_only is not None:
        payload["post_only"] = post_only

    if reduce_only is not None:
        payload["reduce_only"] = reduce_only

    if cancel_order_on_pause is not None:
        payload["cancel_order_on_pause"] = cancel_order_on_pause

    if exchange_index is not None:
        payload["exchange_index"] = exchange_index

    return payload
