from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kalshi_mm_bot.live import run_live_strategy
from kalshi_mm_bot.market.price import parse_count_fp
from kalshi_mm_bot.market.tickers import parse_ticker_tuple
from kalshi_mm_bot.strategy import STRATEGY_NAMES, strategy_from_name


def main() -> None:
    args = _parse_args()

    try:
        stats = asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("Stopped.")
        return

    print(
        "Done: "
        f"events={stats.event_count}, "
        f"book_updates={stats.orderbook_updates}, "
        f"fills={stats.fill_count}, "
        f"creates={stats.create_count}, "
        f"cancels={stats.cancel_count}, "
        f"mode={'dry-run' if stats.dry_run else 'LIVE'}"
    )


async def _run(args: argparse.Namespace):
    strategy = strategy_from_name(
        args.strategy,
        count=parse_count_fp(args.order_size),
        max_position=parse_count_fp(args.max_position),
    )

    return await run_live_strategy(
        tickers=args.tickers,
        strategy=strategy,
        prod=args.prod,
        dry_run=not args.execute,
        duration_seconds=args.duration_sec,
        client_prefix=args.client_prefix,
        min_requote_seconds=args.min_requote_sec,
        cancel_on_stop=not args.leave_orders,
        status=print,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a strategy against live Kalshi markets.")
    parser.add_argument("tickers", nargs="+", help="Market tickers to trade.")
    parser.add_argument(
        "--strategy",
        choices=STRATEGY_NAMES,
        default="adaptive",
        help="Strategy to run. Default: adaptive.",
    )
    parser.add_argument(
        "--order-size",
        default="1.00",
        help="Max contracts per quote. Default: 1.00.",
    )
    parser.add_argument(
        "--max-position",
        default="10.00",
        help="Absolute YES inventory cap. Default: 10.00.",
    )
    parser.add_argument(
        "--duration-sec",
        type=float,
        help="Stop automatically after this many seconds.",
    )
    parser.add_argument(
        "--min-requote-sec",
        type=float,
        default=0.25,
        help="Minimum seconds between replacement creates per market. Default: 0.25.",
    )
    parser.add_argument(
        "--client-prefix",
        default="kmm",
        help="Client order ID prefix used to identify this bot's orders. Default: kmm.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Submit/cancel real orders. Omit for dry-run.",
    )
    parser.add_argument(
        "--leave-orders",
        action="store_true",
        help="Do not cancel this run's live orders on shutdown.",
    )
    parser.add_argument(
        "--confirm-real-money",
        action="store_true",
        help="Required with --prod --execute.",
    )

    environment = parser.add_mutually_exclusive_group()
    environment.add_argument(
        "--demo",
        dest="prod",
        action="store_false",
        default=False,
        help="Use demo endpoints. Default.",
    )
    environment.add_argument(
        "--prod",
        dest="prod",
        action="store_true",
        help="Use production endpoints.",
    )

    args = parser.parse_args()
    args.tickers = parse_ticker_tuple(args.tickers)

    if not args.tickers:
        parser.error("provide at least one market ticker")

    if args.duration_sec is not None and args.duration_sec <= 0:
        parser.error("--duration-sec must be greater than zero")

    if args.min_requote_sec < 0:
        parser.error("--min-requote-sec must be non-negative")

    try:
        if parse_count_fp(args.order_size) <= 0:
            parser.error("--order-size must be greater than zero")

        if parse_count_fp(args.max_position) < 0:
            parser.error("--max-position must be non-negative")
    except ValueError as error:
        parser.error(str(error))

    if args.prod and args.execute and not args.confirm_real_money:
        parser.error("--prod --execute requires --confirm-real-money")

    return args


if __name__ == "__main__":
    main()
