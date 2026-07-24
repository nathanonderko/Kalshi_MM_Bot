from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import deque
from dataclasses import dataclass, field

from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.price import COUNT_SCALE, ONE_DOLLAR
from kalshi_mm_bot.market.types import MarketTicker, OrderAction
from kalshi_mm_bot.strategy.types import PortfolioView, QuoteIntent, StrategyContext

BPS_SCALE = 10_000


@dataclass(slots=True)
class AdaptivePredictionMarketMakerStrategy:
    """Market maker with inventory, fee, liquidity, and short-term trend controls."""

    count: int = COUNT_SCALE
    max_position: int = 10 * COUNT_SCALE
    min_count: int = COUNT_SCALE // 4
    min_profit_edge: int = 25
    fee_rate_bps: int = 700
    max_spread: int = 1_000
    max_quote_away: int = 100
    inventory_skew: int = 300
    inventory_size_penalty_bps: int = 7_500
    liquidity_fraction_bps: int = 5_000
    min_top_size: int = COUNT_SCALE // 4
    adverse_move_threshold: int = 100
    trend_lookback: int = 4
    name: str = "adaptive_prediction_mm"

    _mid_history: dict[MarketTicker, deque[int]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.count <= 0:
            raise ValueError("count must be greater than zero")
        if self.max_position < 0:
            raise ValueError("max_position must be non-negative")
        if self.min_count <= 0:
            raise ValueError("min_count must be greater than zero")
        if self.trend_lookback <= 1:
            raise ValueError("trend_lookback must be greater than one")

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

        spread = best_ask - best_bid

        if spread > self.max_spread:
            self._record_mid(market_ticker, best_bid, best_ask)
            return ()

        position = portfolio.position(market_ticker)
        mid = (best_bid + best_ask) // 2
        trend = self._record_mid(market_ticker, best_bid, best_ask)
        reservation_price = _clamp(
            mid - _inventory_offset(position, self.max_position, self.inventory_skew),
            0,
            ONE_DOLLAR,
        )
        required_edge = self.min_profit_edge + _fee_edge(mid, self.fee_rate_bps)

        intents: list[QuoteIntent] = []

        buy_price = self._quote_price(
            orderbook,
            best_quote=best_bid,
            limit_price=reservation_price - required_edge,
            action="buy",
        )
        if buy_price is not None and not _blocks_buy(trend, self.adverse_move_threshold):
            buy_count = self._quote_count(
                orderbook,
                best_price=best_bid,
                action="buy",
                position=position,
            )

            if buy_count > 0:
                intents.append(_quote(market_ticker, "buy", buy_price, buy_count))

        sell_price = self._quote_price(
            orderbook,
            best_quote=best_ask,
            limit_price=reservation_price + required_edge,
            action="sell",
        )
        if sell_price is not None and not _blocks_sell(trend, self.adverse_move_threshold):
            sell_count = self._quote_count(
                orderbook,
                best_price=best_ask,
                action="sell",
                position=position,
            )

            if sell_count > 0:
                intents.append(_quote(market_ticker, "sell", sell_price, sell_count))

        return tuple(intents)

    def _record_mid(self, market_ticker: MarketTicker, best_bid: int, best_ask: int) -> int:
        history = self._mid_history.setdefault(
            market_ticker,
            deque(maxlen=self.trend_lookback),
        )
        mid = (best_bid + best_ask) // 2
        history.append(mid)
        return mid - history[0] if len(history) == history.maxlen else 0

    def _quote_price(
        self,
        orderbook: Orderbook,
        *,
        best_quote: int,
        limit_price: int,
        action: OrderAction,
    ) -> int | None:
        if action == "buy":
            price = _floor_level(orderbook.price_levels, min(best_quote, limit_price))
            return (
                price
                if price is not None and best_quote - price <= self.max_quote_away
                else None
            )

        price = _ceil_level(orderbook.price_levels, max(best_quote, limit_price))
        return (
            price
            if price is not None and price - best_quote <= self.max_quote_away
            else None
        )

    def _quote_count(
        self,
        orderbook: Orderbook,
        *,
        best_price: int,
        action: OrderAction,
        position: int,
    ) -> int:
        levels = orderbook.bids if action == "buy" else orderbook.asks
        top_size = levels[best_price]

        if top_size < self.min_top_size:
            return 0

        min_count = min(self.min_count, self.count)
        liquidity_count = top_size * self.liquidity_fraction_bps // BPS_SCALE
        desired = min(self.count, max(min_count, liquidity_count))
        desired = _apply_inventory_size_penalty(
            desired,
            action=action,
            position=position,
            max_position=self.max_position,
            penalty_bps=self.inventory_size_penalty_bps,
        )
        capacity = self.max_position - position if action == "buy" else self.max_position + position
        count = min(desired, capacity)

        return count if count >= min_count else 0


def _quote(
    market_ticker: MarketTicker,
    action: OrderAction,
    yes_price: int,
    count: int,
) -> QuoteIntent:
    return QuoteIntent(
        quote_id=f"{market_ticker}:adaptive:yes:{action}",
        market_ticker=market_ticker,
        action=action,
        side="yes",
        yes_price=yes_price,
        count=count,
    )


def _fee_edge(mid_price: int, fee_rate_bps: int) -> int:
    if fee_rate_bps <= 0:
        return 0

    return _ceil_div(
        fee_rate_bps * mid_price * (ONE_DOLLAR - mid_price),
        ONE_DOLLAR * BPS_SCALE,
    )


def _inventory_offset(position: int, max_position: int, max_skew: int) -> int:
    if max_position <= 0 or max_skew <= 0:
        return 0

    position = _clamp(position, -max_position, max_position)
    return position * max_skew // max_position


def _apply_inventory_size_penalty(
    count: int,
    *,
    action: OrderAction,
    position: int,
    max_position: int,
    penalty_bps: int,
) -> int:
    if max_position <= 0 or penalty_bps <= 0:
        return count

    risk_increasing_position = position if action == "buy" else -position

    if risk_increasing_position <= 0:
        return count

    penalty = min(BPS_SCALE, penalty_bps * risk_increasing_position // max_position)
    return count * (BPS_SCALE - penalty) // BPS_SCALE


def _blocks_buy(trend: int, threshold: int) -> bool:
    return threshold > 0 and trend <= -threshold


def _blocks_sell(trend: int, threshold: int) -> bool:
    return threshold > 0 and trend >= threshold


def _floor_level(levels: tuple[int, ...], price: int) -> int | None:
    index = bisect_right(levels, price) - 1
    return levels[index] if index >= 0 else None


def _ceil_level(levels: tuple[int, ...], price: int) -> int | None:
    index = bisect_left(levels, price)
    return levels[index] if index < len(levels) else None


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _clamp(value: int, lower: int, upper: int) -> int:
    return min(max(value, lower), upper)
