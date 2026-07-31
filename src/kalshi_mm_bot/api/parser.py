from collections.abc import Callable
from typing import Any

from kalshi_mm_bot.market.price import parse_count_fp, parse_money_fp, parse_price_fp
from kalshi_mm_bot.market.types import MarketPosition, OrderFill, PriceRange


ParsedWsMessage = OrderFill | MarketPosition


def parse_ws_message(msg: dict[str, Any]) -> ParsedWsMessage | None:
    msg_type = msg.get("type")

    if msg_type == "fill":
        return parse_order_fill(msg)

    if msg_type == "market_position":
        return parse_market_position(msg)

    return None


def parse_price_ranges(raw_market: dict[str, Any]) -> tuple[PriceRange, ...]:
    return tuple(
        PriceRange(
            start=_parse_field(parse_price_fp, price_range, "start"),
            end=_parse_field(parse_price_fp, price_range, "end"),
            step=_parse_field(parse_price_fp, price_range, "step"),
        )
        for price_range in raw_market["price_ranges"]
    )


def parse_order_fill(msg: dict[str, Any]) -> OrderFill:
    data = msg["msg"]

    return OrderFill(
        trade_id=data["trade_id"],
        order_id=data["order_id"],
        market_ticker=data["market_ticker"],
        is_taker=data["is_taker"],
        side=data["side"],
        action=data["action"],
        yes_price=_parse_field(parse_price_fp, data, "yes_price_dollars"),
        count=_parse_field(parse_count_fp, data, "count_fp"),
        post_position=_parse_field(parse_count_fp, data, "post_position_fp"),
    )


def parse_market_position(msg: dict[str, Any]) -> MarketPosition:
    data = msg["msg"]

    return MarketPosition(
        market_ticker=data["market_ticker"],
        position=_parse_field(parse_count_fp, data, "position_fp"),
        position_cost=_parse_field(parse_money_fp, data, "position_cost_dollars"),
        realized_pnl=_parse_field(parse_money_fp, data, "realized_pnl_dollars"),
        fees_paid=_parse_field(parse_money_fp, data, "fees_paid_dollars"),
        volume=_parse_field(parse_count_fp, data, "volume_fp"),
    )


def _parse_field(
    parser: Callable[[str], int],
    data: dict[str, Any],
    field: str,
) -> int:
    value = data[field]

    try:
        return parser(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field}={value!r}: {error}") from error
