from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.price import COUNT_SCALE, parse_count_fp, parse_price_fp
from kalshi_mm_bot.market.types import PriceRange
from kalshi_mm_bot.sim import SimPortfolio
from kalshi_mm_bot.strategy import (
    AdaptivePredictionMarketMakerStrategy,
    parse_adaptive_params,
    strategy_from_name,
)
from kalshi_mm_bot.strategy.types import StrategyContext

PRICE_RANGES = (PriceRange(start=0, end=10000, step=100),)


def make_book(
    bid: str = "0.5000",
    ask: str = "0.5400",
    *,
    bid_size: str = "1.00",
    ask_size: str = "1.00",
) -> Orderbook:
    return Orderbook.from_snapshot(
        market_ticker="M1",
        seq=1,
        bids_raw=((bid, bid_size),),
        asks_raw=((ask, ask_size),),
        price_ranges=PRICE_RANGES,
    )


def test_adaptive_strategy_quotes_only_when_spread_covers_fee_buffer() -> None:
    strategy = AdaptivePredictionMarketMakerStrategy()
    context = StrategyContext(event_count=1, offset_seconds=0)

    wide_intents = strategy.on_orderbook(context, "M1", make_book(), SimPortfolio())
    tight_intents = strategy.on_orderbook(
        context,
        "M2",
        make_book("0.5000", "0.5100"),
        SimPortfolio(),
    )

    assert [(intent.action, intent.yes_price) for intent in wide_intents] == [
        ("buy", parse_price_fp("0.5000")),
        ("sell", parse_price_fp("0.5400")),
    ]
    assert tight_intents == ()


def test_adaptive_strategy_skews_quotes_away_from_more_inventory() -> None:
    portfolio = SimPortfolio()
    portfolio.positions["M1"] = 9 * COUNT_SCALE
    strategy = AdaptivePredictionMarketMakerStrategy()

    intents = strategy.on_orderbook(
        StrategyContext(event_count=1, offset_seconds=0),
        "M1",
        make_book(),
        portfolio,
    )

    assert [intent.action for intent in intents] == ["sell"]


def test_adaptive_strategy_blocks_sells_into_sharp_up_move() -> None:
    strategy = AdaptivePredictionMarketMakerStrategy(trend_lookback=2)
    portfolio = SimPortfolio()

    strategy.on_orderbook(
        StrategyContext(event_count=1, offset_seconds=0),
        "M1",
        make_book("0.5000", "0.5400"),
        portfolio,
    )
    intents = strategy.on_orderbook(
        StrategyContext(event_count=2, offset_seconds=1),
        "M1",
        make_book("0.5200", "0.5600"),
        portfolio,
    )

    assert [intent.action for intent in intents] == ["buy"]


def test_adaptive_strategy_sizes_from_displayed_liquidity() -> None:
    strategy = AdaptivePredictionMarketMakerStrategy()
    intents = strategy.on_orderbook(
        StrategyContext(event_count=1, offset_seconds=0),
        "M1",
        make_book(bid_size="0.60", ask_size="0.60"),
        SimPortfolio(),
    )

    assert [intent.count for intent in intents] == [parse_count_fp("0.30")] * 2


def test_strategy_factory_keeps_dumb_benchmark_selectable() -> None:
    adaptive = strategy_from_name(
        "adaptive",
        count=COUNT_SCALE,
        max_position=10 * COUNT_SCALE,
    )
    dumb = strategy_from_name(
        "dumb",
        count=COUNT_SCALE,
        max_position=10 * COUNT_SCALE,
    )

    assert adaptive.name == "adaptive_prediction_mm"
    assert dumb.name == "dumb_join_top"


def test_parse_adaptive_params_accepts_human_scale_values() -> None:
    params = parse_adaptive_params(
        "min_count=0.50,min_profit_edge=0.0030,liquidity_fraction_bps=2500"
    )

    assert params == {
        "min_count": parse_count_fp("0.50"),
        "min_profit_edge": parse_price_fp("0.0030"),
        "liquidity_fraction_bps": 2500,
    }


def test_strategy_factory_applies_adaptive_overrides() -> None:
    strategy = strategy_from_name(
        "adaptive",
        count=COUNT_SCALE,
        max_position=10 * COUNT_SCALE,
        adaptive_params={"min_profit_edge": parse_price_fp("0.0100")},
    )

    assert isinstance(strategy, AdaptivePredictionMarketMakerStrategy)
    assert strategy.min_profit_edge == parse_price_fp("0.0100")
