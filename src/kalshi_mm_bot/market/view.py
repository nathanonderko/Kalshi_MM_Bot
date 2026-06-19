from collections.abc import Iterable, Mapping

from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.price import format_count_fp, format_price_fp
from kalshi_mm_bot.market.types import BookSide


TopOfBookRow = tuple[str, str, str, str, str]
EMPTY_LEVEL = ("-", "-")


def top_of_book_rows(
    orderbooks: Mapping[str, Orderbook],
    tickers: Iterable[str],
) -> tuple[TopOfBookRow, ...]:
    return tuple(
        _top_of_book_row(ticker, orderbooks.get(ticker))
        for ticker in tickers
    )


def _top_of_book_row(ticker: str, book: Orderbook | None) -> TopOfBookRow:
    bid_price, bid_size = format_best_level(book, "bid")
    ask_price, ask_size = format_best_level(book, "ask")
    return ticker, bid_price, bid_size, ask_price, ask_size


def format_best_level(book: Orderbook | None, side: BookSide) -> tuple[str, str]:
    if book is None:
        return EMPTY_LEVEL

    if side == "bid":
        price = book.best_bid
        levels = book.bids
    else:
        price = book.best_ask
        levels = book.asks

    if price is None:
        return EMPTY_LEVEL

    return format_price_fp(price), format_count_fp(levels[price])
