from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.types import PriceRange


TAPERED_RANGES = (
    PriceRange(start=0, end=1000, step=10),
    PriceRange(start=1000, end=9000, step=100),
    PriceRange(start=9000, end=10000, step=10),
)


def test_orderbook_uses_cached_tapered_price_levels() -> None:
    first = Orderbook.from_snapshot(
        market_ticker="A",
        seq=1,
        bids_raw=(),
        asks_raw=(),
        price_ranges=TAPERED_RANGES,
    )
    second = Orderbook.from_snapshot(
        market_ticker="B",
        seq=1,
        bids_raw=(),
        asks_raw=(),
        price_ranges=TAPERED_RANGES,
    )

    assert first.price_levels is second.price_levels
    assert len(first.price_levels) == 281
    assert 990 in first.price_levels
    assert 1000 in first.price_levels
    assert 1010 not in first.price_levels
    assert 9010 in first.price_levels


def test_orderbook_finds_next_bid_using_tapered_levels() -> None:
    book = Orderbook.from_snapshot(
        market_ticker="TEST",
        seq=1,
        bids_raw=(
            ("0.0900", "1.00"),
            ("0.0990", "1.00"),
            ("0.1000", "1.00"),
        ),
        asks_raw=(),
        price_ranges=TAPERED_RANGES,
    )

    book.apply_delta(seq=2, side="bid", price=1000, delta=-100)

    assert book.best_bid == 990


def test_orderbook_finds_next_ask_using_tapered_levels() -> None:
    book = Orderbook.from_snapshot(
        market_ticker="TEST",
        seq=1,
        bids_raw=(),
        asks_raw=(
            ("0.9000", "1.00"),
            ("0.9010", "1.00"),
            ("0.9100", "1.00"),
        ),
        price_ranges=TAPERED_RANGES,
    )

    book.apply_delta(seq=2, side="ask", price=9000, delta=-100)

    assert book.best_ask == 9010


def test_orderbook_keeps_new_best_bid_after_add() -> None:
    book = Orderbook.from_snapshot(
        market_ticker="TEST",
        seq=1,
        bids_raw=(("0.5000", "1.00"),),
        asks_raw=(),
        price_ranges=(PriceRange(start=0, end=10000, step=10),),
    )

    book.apply_delta(seq=2, side="bid", price=6000, delta=100)

    assert book.best_bid == 6000
