"""
Fixed-point price/count helpers.

Internal representation:
    price: int
        $1.0000 = 10000
        $0.5600 = 5600
        $0.0010 = 10

    count: int
        1.00 contracts = 100
        10.00 contracts = 1000
        0.01 contracts = 1

Kalshi live feeds normally emit price strings with 4 decimals and count
strings with 2 decimals. The parsers keep a shorter-decimal fallback because
the API contract allows prices/counts with fewer decimal places.
"""

PRICE_DECIMALS = 4
PRICE_SCALE = 10_000

COUNT_DECIMALS = 2
COUNT_SCALE = 100

ONE_DOLLAR = PRICE_SCALE

def parse_price_fp(value: str) -> int:
    return _parse_fixed_point(
        value=value,
        scale=PRICE_SCALE,
        decimals=PRICE_DECIMALS,
        label="price",
    )


def parse_count_fp(value: str) -> int:
    return _parse_fixed_point(
        value=value,
        scale=COUNT_SCALE,
        decimals=COUNT_DECIMALS,
        label="count",
    )


def _parse_fixed_point(
    *,
    value: str,
    scale: int,
    decimals: int,
    label: str,
) -> int:
    value = value.strip()

    if not value:
        raise ValueError(f"empty {label}")

    if "." not in value:
        return int(value) * scale

    sign = -1 if value.startswith("-") else 1
    unsigned = value[1:] if sign < 0 else value
    whole_text, _, frac_text = unsigned.partition(".")

    if "." in frac_text:
        raise ValueError(f"invalid {label}: {value!r}")

    if len(frac_text) > decimals:
        raise ValueError(f"too many {label} decimals: {value!r}")

    whole = int(whole_text) if whole_text else 0
    frac = int(frac_text.ljust(decimals, "0")) if frac_text else 0

    return sign * (whole * scale + frac)


def format_price_fp(price: int) -> str:
    validate_price(price)
    return _format_fixed_point(price, PRICE_SCALE, PRICE_DECIMALS)


def format_count_fp(count: int) -> str:
    return _format_fixed_point(count, COUNT_SCALE, COUNT_DECIMALS)


def _format_fixed_point(value: int, scale: int, decimals: int) -> str:
    sign = "-" if value < 0 else ""
    value = abs(value)

    whole = value // scale
    frac = value % scale

    return f"{sign}{whole}.{frac:0{decimals}d}"


def validate_price(price: int) -> None:
    if not 0 <= price <= ONE_DOLLAR:
        raise ValueError(f"price out of range: {price}")
