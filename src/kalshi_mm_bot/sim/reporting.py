from __future__ import annotations

from kalshi_mm_bot.sim.accounting import format_contract_count, format_money_value
from kalshi_mm_bot.sim.backtest import BacktestSummary

SummaryRow = tuple[str, str]


def backtest_summary_rows(summary: BacktestSummary) -> tuple[SummaryRow, ...]:
    return (
        ("Strategy", summary.strategy_name),
        ("Fill model", summary.fill_model),
        *(
            (("Starting balance", _format_cents(summary.starting_balance_cents)),)
            if summary.starting_balance_cents is not None
            else ()
        ),
        ("Events", str(summary.event_count)),
        ("Orders", str(summary.order_count)),
        ("Open orders", str(summary.open_order_count)),
        ("Skipped orders", str(summary.skipped_order_count)),
        ("Reserved risk", _format_cents(summary.reserved_risk_cents)),
        ("Fills", str(summary.fill_count)),
        ("Buy filled", format_contract_count(summary.buy_filled_count)),
        ("Sell filled", format_contract_count(summary.sell_filled_count)),
        ("Position", format_contract_count(summary.position_count)),
        ("Volume", format_contract_count(summary.volume_count)),
        ("Cash", format_money_value(summary.cash_value)),
        ("Mark to market", format_money_value(summary.mark_to_market_value)),
    )


def backtest_summary_lines(summary: BacktestSummary) -> list[str]:
    rows = backtest_summary_rows(summary)
    width = max(len(label) for label, _ in rows)
    return [f"{label.ljust(width)}  {value}" for label, value in rows]


def format_backtest_summary(summary: BacktestSummary) -> str:
    return "\n".join(backtest_summary_lines(summary))


def _format_cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100}.{cents % 100:02d}"
