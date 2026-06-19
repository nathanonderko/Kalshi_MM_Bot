from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.types import PriceRange
from kalshi_mm_bot.market.view import top_of_book_rows


def test_top_of_book_rows_formats_missing_and_present_books() -> None:
    book = Orderbook.from_snapshot(
        market_ticker="M1",
        seq=1,
        bids_raw=(("0.5100", "2.00"),),
        asks_raw=(("0.5200", "3.00"),),
        price_ranges=(PriceRange(start=0, end=10000, step=100),),
    )

    assert top_of_book_rows({"M1": book}, ("M1", "M2")) == (
        ("M1", "0.5100", "2.00", "0.5200", "3.00"),
        ("M2", "-", "-", "-", "-"),
    )
