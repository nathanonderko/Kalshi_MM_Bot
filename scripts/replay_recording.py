from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kalshi_mm_bot.api.feed_controller import FeedController, ORDERBOOK_CHANNEL
from kalshi_mm_bot.market.price import format_price_fp, parse_count_fp
from kalshi_mm_bot.market.view import TopOfBookRow, top_of_book_rows
from kalshi_mm_bot.recording import (
    RecordedRestClient,
    RecordedWebSocketClient,
    RecordingSessionReader,
)
from kalshi_mm_bot.recording.paths import latest_recording_dir, require_recording_path
from kalshi_mm_bot.sim import (
    BacktestUpdate,
    backtest_summary_lines,
    fill_model_from_name,
    format_backtest_summary,
    format_contract_count,
    run_replay_backtest,
)
from kalshi_mm_bot.strategy import STRATEGY_NAMES, strategy_from_name


Row = TopOfBookRow


def main() -> None:
    args = _parse_args()
    asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> None:
    if args.simulate:
        await _run_simulation(args)
        return

    reader = RecordingSessionReader.open(args.recording)
    ws = RecordedWebSocketClient.from_session(reader, speed_multiplier=args.speed)
    rest = RecordedRestClient(reader.manifest)
    controller = FeedController(rest=rest, ws=ws)
    watcher: TerminalBookWatcher | None = None

    try:
        await controller.connect()
        await controller.subscribe(reader.manifest.tickers, channels=reader.manifest.channels)

        if args.watch and ORDERBOOK_CHANNEL in reader.manifest.channels:
            watcher = TerminalBookWatcher(
                tickers=reader.manifest.tickers,
                refresh_interval=args.watch_interval,
            )
            watcher.render(controller, event_count=ws.returned_count)
        elif args.watch:
            print(
                "Watch mode only displays orderbook data. "
                f"Recording channels: {', '.join(reader.manifest.channels)}"
            )

        while True:
            try:
                updated_ticker = await controller.recv()
            except EOFError:
                break

            if watcher is not None and updated_ticker is not None:
                watcher.maybe_render(
                    controller,
                    event_count=ws.returned_count,
                    updated_ticker=updated_ticker,
                )

        rows = top_of_book_rows(controller.orderbooks, reader.manifest.tickers)

        if watcher is not None:
            watcher.render(
                controller,
                event_count=ws.returned_count,
                updated_ticker="EOF",
                final=True,
            )
    finally:
        await controller.close()

    if watcher is not None:
        print("")

    print(f"Replayed {ws.returned_count} event(s) from {reader.directory}")
    print(f"Environment: {reader.manifest.environment}")
    print(f"Channels: {', '.join(reader.manifest.channels)}")

    if ORDERBOOK_CHANNEL not in reader.manifest.channels:
        return

    print("")
    print("Ticker,Best Bid,Bid Size,Best Ask,Ask Size")
    for row in rows:
        print(",".join(row))


async def _run_simulation(args: argparse.Namespace) -> None:
    strategy = strategy_from_name(
        args.strategy,
        count=parse_count_fp(args.order_size),
        max_position=parse_count_fp(args.max_position),
    )
    fill_model = fill_model_from_name(args.fill_model)
    watcher = TerminalBacktestWatcher() if args.watch else None

    result = await run_replay_backtest(
        args.recording,
        strategy=strategy,
        fill_model=fill_model,
        speed_multiplier=args.speed,
        latency_seconds=args.latency_sec,
        on_update=watcher.render if watcher is not None else None,
        update_interval_seconds=args.watch_interval,
    )

    if watcher is not None:
        print("")

    print(f"Simulated {result.summary.event_count} event(s) from {result.recording}")
    print("")
    print(format_backtest_summary(result.summary))

    if result.final_rows:
        print("")
        print("Ticker,Best Bid,Bid Size,Best Ask,Ask Size")
        for row in result.final_rows:
            print(",".join(row))

    if args.print_fills and result.fills:
        print("")
        print("Fill ID,Time,Order ID,Ticker,Action,Side,Price,Count,Model,Reason")
        for fill in result.fills:
            print(
                ",".join(
                    (
                        fill.fill_id,
                        "" if fill.observed_at_utc is None else fill.observed_at_utc,
                        fill.order_id,
                        fill.market_ticker,
                        fill.action,
                        fill.side,
                        format_price_fp(fill.yes_price),
                        format_contract_count(fill.count),
                        fill.fill_model,
                        fill.reason,
                    )
                )
            )


class TerminalBookWatcher:
    def __init__(self, *, tickers: tuple[str, ...], refresh_interval: float) -> None:
        self.tickers = tickers
        self.refresh_interval = refresh_interval
        self._last_render: float | None = None

    def maybe_render(
        self,
        controller: FeedController,
        *,
        event_count: int,
        updated_ticker: str | None = None,
    ) -> None:
        now = time.monotonic()

        if self._last_render is not None and now - self._last_render < self.refresh_interval:
            return

        self.render(
            controller,
            event_count=event_count,
            updated_ticker=updated_ticker,
        )

    def render(
        self,
        controller: FeedController,
        *,
        event_count: int,
        updated_ticker: str | None = None,
        final: bool = False,
    ) -> None:
        self._last_render = time.monotonic()
        rows = top_of_book_rows(controller.orderbooks, self.tickers)
        text = _format_watch_table(
            rows,
            event_count=event_count,
            updated_ticker=updated_ticker,
            final=final,
        )
        prefix = "\x1b[2J\x1b[H" if sys.stdout.isatty() else ""

        print(prefix + text, end="", flush=True)


class TerminalBacktestWatcher:
    def render(self, update: BacktestUpdate) -> None:
        text = _format_backtest_watch(update)
        prefix = "\x1b[2J\x1b[H" if sys.stdout.isatty() else ""
        print(prefix + text, end="", flush=True)


def _format_backtest_watch(update: BacktestUpdate) -> str:
    status = "FINAL" if update.final else "LIVE"
    summary = update.summary
    header = (
        f"Backtest watch: {status} | events={summary.event_count} | "
        f"fills={summary.fill_count} | updated={update.updated_ticker or '-'}"
    )
    lines = [header, ""]
    lines.extend(backtest_summary_lines(summary))

    if update.rows:
        lines.append("")
        table_rows = [("Ticker", "Best Bid", "Bid Size", "Best Ask", "Ask Size"), *update.rows]
        widths = [
            max(len(row[column]) for row in table_rows)
            for column in range(len(table_rows[0]))
        ]
        lines.append(_format_table_row(table_rows[0], widths))
        lines.append("  ".join("-" * width for width in widths))

        for row in update.rows:
            lines.append(_format_table_row(row, widths))

    if update.recent_fills:
        lines.append("")
        lines.append("Recent fills:")

        for fill in update.recent_fills[-5:]:
            lines.append(
                "  "
                f"{fill.action} {format_contract_count(fill.count)} "
                f"{fill.market_ticker} @ {format_price_fp(fill.yes_price)} "
                f"({fill.reason})"
            )

    lines.append("")
    return "\n".join(lines)


def _format_watch_table(
    rows: tuple[Row, ...],
    *,
    event_count: int,
    updated_ticker: str | None,
    final: bool,
) -> str:
    status = "FINAL" if final else "LIVE"
    updated = "-" if updated_ticker is None else updated_ticker
    header = f"Replay watch: {status} | events={event_count} | updated={updated}"

    table_rows = [("Ticker", "Best Bid", "Bid Size", "Best Ask", "Ask Size"), *rows]
    widths = [
        max(len(row[column]) for row in table_rows)
        for column in range(len(table_rows[0]))
    ]

    lines = [header, ""]
    lines.append(_format_table_row(table_rows[0], widths))
    lines.append("  ".join("-" * width for width in widths))

    for row in rows:
        lines.append(_format_table_row(row, widths))

    lines.append("")
    return "\n".join(lines)


def _format_table_row(row: tuple[str, ...], widths: list[int]) -> str:
    return "  ".join(
        value.ljust(widths[index]) if index == 0 else value.rjust(widths[index])
        for index, value in enumerate(row)
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay a recorded Kalshi market-data session.")
    parser.add_argument(
        "recording",
        nargs="?",
        type=Path,
        help=(
            "Recording directory containing manifest.json. If omitted, you will be "
            "prompted; blank input uses the newest recording."
        ),
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=None,
        help=(
            "Replay speed multiplier. 0 means as fast as possible. "
            "Default: 0, or 1 with --watch."
        ),
    )
    parser.add_argument(
        "--realtime",
        action="store_true",
        help="Replay using original event timing; equivalent to --speed 1.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Continuously render top-of-book while replaying.",
    )
    parser.add_argument(
        "--watch-interval",
        type=float,
        default=0.25,
        help="Minimum seconds between watch table refreshes. Default: 0.25.",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Run a market-maker simulation while replaying.",
    )
    parser.add_argument(
        "--strategy",
        choices=STRATEGY_NAMES,
        default="adaptive",
        help="Strategy used with --simulate. Default: adaptive.",
    )
    parser.add_argument(
        "--fill-model",
        choices=("optimistic", "pessimistic", "queue"),
        default="queue",
        help="Fill model used with --simulate. Default: queue.",
    )
    parser.add_argument(
        "--order-size",
        default="1.00",
        help="Contracts per quote for --simulate. Default: 1.00.",
    )
    parser.add_argument(
        "--max-position",
        default="10.00",
        help="Absolute YES inventory cap for --simulate. Default: 10.00.",
    )
    parser.add_argument(
        "--latency-sec",
        type=float,
        default=0.0,
        help="Simulated open/cancel latency in seconds. Default: 0.",
    )
    parser.add_argument(
        "--print-fills",
        action="store_true",
        help="Print simulated fills as CSV after --simulate completes.",
    )
    args = parser.parse_args()

    if args.speed is not None and args.speed < 0:
        parser.error("--speed must be non-negative")

    if args.watch_interval <= 0:
        parser.error("--watch-interval must be greater than zero")

    if args.latency_sec < 0:
        parser.error("--latency-sec must be non-negative")

    try:
        if parse_count_fp(args.order_size) <= 0:
            parser.error("--order-size must be greater than zero")

        if parse_count_fp(args.max_position) < 0:
            parser.error("--max-position must be non-negative")
    except ValueError as error:
        parser.error(str(error))

    args.speed = _speed_multiplier(args)
    args.recording = _recording_path(args.recording, parser)
    return args


def _speed_multiplier(args: argparse.Namespace) -> float:
    if args.realtime:
        return 1.0

    if args.speed is not None:
        return args.speed

    return 1.0 if args.watch else 0.0


def _recording_path(
    raw_path: Path | None,
    parser: argparse.ArgumentParser,
) -> Path:
    if raw_path is None:
        try:
            raw_text = input("Recording directory (blank for newest): ").strip()
        except EOFError:
            parser.error("provide a recording directory")

        raw_path = latest_recording_dir(ROOT) if not raw_text else Path(raw_text)

    try:
        return require_recording_path(raw_path, root=ROOT)
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
