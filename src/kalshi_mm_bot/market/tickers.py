from __future__ import annotations

import re
from collections.abc import Iterable

_TICKER_SEPARATOR_RE = re.compile(r"[\s,]+")


def parse_ticker_tuple(raw_values: str | Iterable[str]) -> tuple[str, ...]:
    values = (raw_values,) if isinstance(raw_values, str) else raw_values
    tickers: list[str] = []

    for value in values:
        tickers.extend(
            ticker.upper()
            for ticker in _TICKER_SEPARATOR_RE.split(value.strip())
            if ticker
        )

    return tuple(dict.fromkeys(tickers))
