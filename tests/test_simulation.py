import asyncio

import pytest

from kalshi_mm_bot.api.feed_controller import ORDERBOOK_CHANNEL
from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.price import COUNT_SCALE, parse_count_fp, parse_price_fp
from kalshi_mm_bot.market.types import PriceRange
from kalshi_mm_bot.recording import RecordingManifest, RecordingSessionWriter
from kalshi_mm_bot.sim import (
    OptimisticFillModel,
    PessimisticFillModel,
    QueueAwareFillModel,
    SimPortfolio,
    SimulatedOrderManager,
    optimize_adaptive_backtest,
    run_replay_backtest,
)
from kalshi_mm_bot.strategy import DumbMarketMakerStrategy
from kalshi_mm_bot.strategy.requote import RequotePolicy
from kalshi_mm_bot.strategy.types import QuoteIntent, StrategyContext


PRICE_RANGES = {"M1": (PriceRange(start=0, end=10000, step=100),)}


def snapshot(seq: int, bid_size: str = "1.00", ask_size: str = "1.00") -> dict:
    return {
        "type": "orderbook_snapshot",
        "sid": 10,
        "seq": seq,
        "msg": {
            "market_ticker": "M1",
            "yes_dollars_fp": [["0.5000", bid_size]],
            "no_dollars_fp": [["0.5100", ask_size]],
        },
    }


def delta(seq: int, side: str, price: str, amount: str) -> dict:
    return {
        "type": "orderbook_delta",
        "sid": 10,
        "seq": seq,
        "msg": {
            "market_ticker": "M1",
            "side": side,
            "price_dollars": price,
            "delta_fp": amount,
        },
    }


def subscribed(command_id: int = 1) -> dict:
    return {
        "id": command_id,
        "type": "subscribed",
        "msg": {"channel": ORDERBOOK_CHANNEL, "sid": 10},
    }


def make_book() -> Orderbook:
    return Orderbook.from_snapshot(
        market_ticker="M1",
        seq=1,
        bids_raw=(("0.5000", "1.00"),),
        asks_raw=(("0.5100", "1.00"),),
        price_ranges=PRICE_RANGES["M1"],
    )


def buy_intent(price: str = "0.5000", count: int = COUNT_SCALE) -> QuoteIntent:
    return QuoteIntent(
        quote_id="M1:yes:buy",
        market_ticker="M1",
        action="buy",
        side="yes",
        yes_price=parse_price_fp(price),
        count=count,
    )


def test_dumb_market_maker_quotes_top_of_book_and_respects_position_limit() -> None:
    book = make_book()
    portfolio = SimPortfolio()
    strategy = DumbMarketMakerStrategy(count=COUNT_SCALE, max_position=COUNT_SCALE)
    context = StrategyContext(event_count=1, offset_seconds=0)

    intents = strategy.on_orderbook(context, "M1", book, portfolio)

    assert [(intent.action, intent.yes_price) for intent in intents] == [
        ("buy", parse_price_fp("0.5000")),
        ("sell", parse_price_fp("0.5100")),
    ]

    portfolio.positions["M1"] = COUNT_SCALE

    limited = strategy.on_orderbook(context, "M1", book, portfolio)

    assert [intent.action for intent in limited] == ["sell"]


def test_optimistic_fill_model_fills_same_level_reduction() -> None:
    book = make_book()
    manager = SimulatedOrderManager(
        fill_model=OptimisticFillModel(),
        portfolio=SimPortfolio(),
    )
    manager.place_order(
        buy_intent(),
        {"M1": book},
        StrategyContext(event_count=1, offset_seconds=0),
    )

    book.apply_delta(seq=2, side="bid", price=parse_price_fp("0.5000"), delta=-50)
    fills = manager.process_market_event(
        delta(2, "yes", "0.5000", "-0.50"),
        {"M1": book},
        StrategyContext(event_count=2, offset_seconds=1),
    )

    assert len(fills) == 1
    assert fills[0].count == parse_count_fp("0.50")
    assert manager.portfolio.position("M1") == parse_count_fp("0.50")


def test_pessimistic_fill_model_waits_until_price_goes_through() -> None:
    book = make_book()
    manager = SimulatedOrderManager(
        fill_model=PessimisticFillModel(),
        portfolio=SimPortfolio(),
    )
    manager.place_order(
        buy_intent(),
        {"M1": book},
        StrategyContext(event_count=1, offset_seconds=0),
    )

    book.apply_delta(seq=2, side="bid", price=parse_price_fp("0.5000"), delta=-50)
    first = manager.process_market_event(
        delta(2, "yes", "0.5000", "-0.50"),
        {"M1": book},
        StrategyContext(event_count=2, offset_seconds=1),
    )

    book.apply_delta(seq=3, side="bid", price=parse_price_fp("0.5000"), delta=-50)
    second = manager.process_market_event(
        delta(3, "yes", "0.5000", "-0.50"),
        {"M1": book},
        StrategyContext(event_count=3, offset_seconds=2),
    )

    assert first == ()
    assert len(second) == 1
    assert second[0].count == COUNT_SCALE


def test_queue_fill_model_accounts_for_queue_ahead() -> None:
    book = make_book()
    manager = SimulatedOrderManager(
        fill_model=QueueAwareFillModel(trade_fraction=1.0),
        portfolio=SimPortfolio(),
    )
    manager.place_order(
        buy_intent(),
        {"M1": book},
        StrategyContext(event_count=1, offset_seconds=0),
    )

    book.apply_delta(seq=2, side="bid", price=parse_price_fp("0.5000"), delta=-50)
    first = manager.process_market_event(
        delta(2, "yes", "0.5000", "-0.50"),
        {"M1": book},
        StrategyContext(event_count=2, offset_seconds=1),
    )

    book.apply_delta(seq=3, side="bid", price=parse_price_fp("0.5000"), delta=-50)
    second = manager.process_market_event(
        delta(3, "yes", "0.5000", "-0.50"),
        {"M1": book},
        StrategyContext(event_count=3, offset_seconds=2),
    )

    assert first == ()
    assert len(second) == 1
    assert second[0].reason == "queue_through"


def test_simulated_order_manager_rejects_duplicate_quote_ids() -> None:
    book = make_book()
    manager = SimulatedOrderManager(
        fill_model=OptimisticFillModel(),
        portfolio=SimPortfolio(),
    )
    context = StrategyContext(event_count=1, offset_seconds=0)

    with pytest.raises(ValueError, match="duplicate quote_id"):
        manager.sync_market_quotes(
            "M1",
            [buy_intent(), buy_intent(count=COUNT_SCALE // 2)],
            {"M1": book},
            context,
        )

    assert manager.orders == {}


def test_simulated_order_manager_keeps_changed_quote_inside_requote_interval() -> None:
    book = make_book()
    manager = SimulatedOrderManager(
        fill_model=OptimisticFillModel(),
        portfolio=SimPortfolio(),
        requote_policy=RequotePolicy(min_requote_seconds=10),
    )

    manager.sync_market_quotes(
        "M1",
        [buy_intent()],
        {"M1": book},
        StrategyContext(event_count=1, offset_seconds=1),
    )
    manager.sync_market_quotes(
        "M1",
        [buy_intent("0.4900")],
        {"M1": book},
        StrategyContext(event_count=2, offset_seconds=2),
    )

    assert [order.yes_price for order in manager.orders.values()] == [parse_price_fp("0.5000")]


def test_simulated_order_manager_skips_orders_over_balance() -> None:
    manager = SimulatedOrderManager(
        fill_model=OptimisticFillModel(),
        portfolio=SimPortfolio(),
        starting_balance_cents=0,
    )

    order = manager.place_order(
        buy_intent(),
        {"M1": make_book()},
        StrategyContext(event_count=1, offset_seconds=0),
    )

    assert order is None
    assert manager.skipped_order_count == 1


def test_replay_backtest_runs_strategy_against_recording(tmp_path) -> None:
    recording_dir = tmp_path / "session"

    with RecordingSessionWriter.create(recording_dir) as writer:
        writer.write_event(subscribed())
        writer.write_event(snapshot(1))
        writer.write_event(delta(2, "yes", "0.5000", "-1.00"))
        writer.write_manifest(
            RecordingManifest.create(
                environment="demo",
                tickers=("M1",),
                channels=(ORDERBOOK_CHANNEL,),
                price_ranges_by_ticker=PRICE_RANGES,
                event_file=writer.event_path.name,
                started_at_utc=writer.started_at_utc,
            )
        )

    async def run() -> None:
        result = await run_replay_backtest(
            recording_dir,
            strategy=DumbMarketMakerStrategy(count=COUNT_SCALE, max_position=COUNT_SCALE),
            fill_model=OptimisticFillModel(),
        )

        assert result.summary.event_count == 3
        assert result.summary.fill_count == 1
        assert result.summary.buy_filled_count == COUNT_SCALE
        assert result.summary.position_count == COUNT_SCALE

    asyncio.run(run())


def test_optimizer_searches_execution_settings_with_balance(tmp_path) -> None:
    recording_dir = tmp_path / "session"

    with RecordingSessionWriter.create(recording_dir) as writer:
        writer.write_event(subscribed())
        writer.write_event(snapshot(1))
        writer.write_event(delta(2, "yes", "0.5000", "-1.00"))
        writer.write_manifest(
            RecordingManifest.create(
                environment="demo",
                tickers=("M1",),
                channels=(ORDERBOOK_CHANNEL,),
                price_ranges_by_ticker=PRICE_RANGES,
                event_file=writer.event_path.name,
                started_at_utc=writer.started_at_utc,
            )
        )

    async def run() -> None:
        result = await optimize_adaptive_backtest(
            recording_dir,
            count=COUNT_SCALE,
            max_position=COUNT_SCALE,
            fill_model_factory=OptimisticFillModel,
            search_space={"min_profit_edge": (25,)},
            execution_search_space={
                "order_size": (parse_count_fp("0.50"), COUNT_SCALE),
                "max_position": (COUNT_SCALE,),
                "min_requote_sec": (0.0,),
                "min_order_rest_sec": (0.0,),
                "requote_price_threshold": (0,),
                "requote_size_threshold_bps": (0,),
            },
            optimize_execution=True,
            starting_balance_cents=100,
            max_trials=None,
        )

        assert len(result.trials) == 2
        assert {trial.settings.count for trial in result.trials} == {
            parse_count_fp("0.50"),
            COUNT_SCALE,
        }

    asyncio.run(run())
