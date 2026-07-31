from kalshi_mm_bot.api.parser import parse_market_position, parse_price_ranges
from kalshi_mm_bot.market.types import MarketPosition, PriceRange


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


def test_parse_market_position_accepts_six_decimal_money_fields() -> None:
    position = parse_market_position(
        {
            "type": "market_position",
            "msg": {
                "market_ticker": "TEST",
                "position_fp": "0.25",
                "position_cost_dollars": "0.017450",
                "realized_pnl_dollars": "-0.017450",
                "fees_paid_dollars": "0.017450",
                "volume_fp": "1.25",
            },
        }
    )

    assert position == MarketPosition(
        market_ticker="TEST",
        position=25,
        position_cost=17450,
        realized_pnl=-17450,
        fees_paid=17450,
        volume=125,
    )
