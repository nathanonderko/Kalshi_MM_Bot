from __future__ import annotations

from collections.abc import Iterable

from kalshi_mm_bot.market.price import format_price_fp
from kalshi_mm_bot.market.types import order_book_side
from kalshi_mm_bot.strategy.types import QuoteIntent


def quote_intent_map(intents: Iterable[QuoteIntent]) -> dict[str, QuoteIntent]:
    wanted: dict[str, QuoteIntent] = {}

    for intent in intents:
        validate_quote_intent(intent)

        if intent.quote_id in wanted:
            raise ValueError(f"duplicate quote_id: {intent.quote_id}")

        wanted[intent.quote_id] = intent

    return wanted


def validate_quote_intent(intent: QuoteIntent) -> None:
    if intent.count <= 0:
        raise ValueError(f"quote count must be positive: {intent.quote_id}")

    format_price_fp(intent.yes_price)
    order_book_side(intent.action, intent.side)
