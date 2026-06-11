from kalshi_mm_bot.api.parser import parse_price_ranges
from kalshi_mm_bot.market.types import PriceRange


def test_parse_price_ranges() -> None:
    price_ranges = parse_price_ranges(
        {
            "ticker": "TEST",
            "price_level_structure": "tapered_deci_cent",
            "price_ranges": [
                {"start": "0.0000", "end": "0.1000", "step": "0.0010"},
                {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
                {"start": "0.9000", "end": "1.0000", "step": "0.0010"},
            ],
        }
    )

    assert price_ranges == (
        PriceRange(start=0, end=1000, step=10),
        PriceRange(start=1000, end=9000, step=100),
        PriceRange(start=9000, end=10000, step=10),
    )
