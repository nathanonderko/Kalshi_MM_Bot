import pytest

from kalshi_mm_bot.market.price import parse_count_fp, parse_money_fp, parse_price_fp


def test_parse_price_fp_accepts_shorter_decimals() -> None:
    assert parse_price_fp("0.9600") == 9600
    assert parse_price_fp("0.960") == 9600
    assert parse_price_fp("9.6") == 96000
    assert parse_price_fp("1") == 10000


def test_parse_price_fp_accepts_extra_trailing_zeros() -> None:
    assert parse_price_fp("0.122500") == 1225


def test_parse_money_fp_accepts_six_decimals() -> None:
    assert parse_money_fp("0.017450") == 17450
    assert parse_money_fp("-1.234567") == -1234567


def test_parse_price_fp_accepts_signed_dollar_amounts() -> None:
    assert parse_price_fp("-1.2300") == -12300


def test_parse_count_fp_uses_count_scale() -> None:
    assert parse_count_fp("42.26") == 4226
    assert parse_count_fp("-42.26") == -4226
    assert parse_count_fp("7.4") == 740
    assert parse_count_fp("7") == 700


def test_fixed_point_parsers_reject_too_many_decimals() -> None:
    assert parse_price_fp("0.12340") == 1234

    with pytest.raises(ValueError):
        parse_price_fp("0.12341")

    with pytest.raises(ValueError):
        parse_money_fp("0.1234561")

    with pytest.raises(ValueError):
        parse_count_fp("1.234")
