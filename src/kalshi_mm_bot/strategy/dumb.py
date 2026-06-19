from __future__ import annotations

from dataclasses import dataclass

from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.price import COUNT_SCALE
from kalshi_mm_bot.market.types import MarketTicker
from kalshi_mm_bot.strategy.types import PortfolioView, QuoteIntent, StrategyContext


@dataclass(frozen=True, slots=True)
class DumbMarketMakerStrategy:
    """Maintains one buy quote at best bid and one sell quote at best ask."""

    count: int = COUNT_SCALE
    max_position: int = 10 * COUNT_SCALE
    name: str = "dumb_join_top"

    def on_orderbook(
        self,
        context: StrategyContext,
        market_ticker: MarketTicker,
        orderbook: Orderbook,
        portfolio: PortfolioView,
    ) -> tuple[QuoteIntent, ...]:
        del context

        best_bid = orderbook.best_bid
        best_ask = orderbook.best_ask

        if best_bid is None or best_ask is None or best_bid >= best_ask:
            return ()

        position = portfolio.position(market_ticker)
        intents: list[QuoteIntent] = []

        if position + self.count <= self.max_position:
            intents.append(
                QuoteIntent(
                    quote_id=f"{market_ticker}:yes:buy",
                    market_ticker=market_ticker,
                    action="buy",
                    side="yes",
                    yes_price=best_bid,
                    count=self.count,
                )
            )

        if position - self.count >= -self.max_position:
            intents.append(
                QuoteIntent(
                    quote_id=f"{market_ticker}:yes:sell",
                    market_ticker=market_ticker,
                    action="sell",
                    side="yes",
                    yes_price=best_ask,
                    count=self.count,
                )
            )

        return tuple(intents)
