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

_PRICE_FRAC_MULTIPLIERS = (PRICE_SCALE, 1_000, 100, 10, 1)
_COUNT_FRAC_MULTIPLIERS = (COUNT_SCALE, 10, 1)


def parse_price_fp(value: str) -> int:
    return _parse_fixed_point(
        value=value,
        scale=PRICE_SCALE,
        decimals=PRICE_DECIMALS,
        frac_multipliers=_PRICE_FRAC_MULTIPLIERS,
        label="price",
    )


def parse_count_fp(value: str) -> int:
    return _parse_fixed_point(
        value=value,
        scale=COUNT_SCALE,
        decimals=COUNT_DECIMALS,
        frac_multipliers=_COUNT_FRAC_MULTIPLIERS,
        label="count",
    )


def _parse_fixed_point(
    *,
    value: str,
    scale: int,
    decimals: int,
    frac_multipliers: tuple[int, ...],
    label: str,
) -> int:
    dot_index = value.find(".")

    if dot_index < 0:
        return int(value) * scale

    frac_len = len(value) - dot_index - 1

    if frac_len > decimals:
        raise ValueError(f"too many {label} decimals: {value!r}")

    if frac_len == decimals:
        return int(value.replace(".", ""))

    sign = -1 if value[0] == "-" else 1
    start = 1 if sign < 0 else 0
    whole = int(value[start:dot_index]) if dot_index > start else 0
    frac = int(value[dot_index + 1:]) if frac_len else 0

    return sign * (whole * scale + frac * frac_multipliers[frac_len])


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
