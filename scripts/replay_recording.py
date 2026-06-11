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
from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.price import format_count_fp, format_price_fp
from kalshi_mm_bot.recording import (
    RecordedRestClient,
    RecordedWebSocketClient,
    RecordingSessionReader,
)


Row = tuple[str, str, str, str, str]


def main() -> None:
    args = _parse_args()
    asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> None:
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

        rows = _snapshot_rows(controller, reader.manifest.tickers)

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


def _snapshot_rows(controller: FeedController, tickers: tuple[str, ...]) -> tuple[Row, ...]:
    rows: list[Row] = []

    for ticker in tickers:
        book = controller.orderbooks.get(ticker)
        bid_price, bid_size = _best_level(book, "bid")
        ask_price, ask_size = _best_level(book, "ask")
        rows.append((ticker, bid_price, bid_size, ask_price, ask_size))

    return tuple(rows)


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
        rows = _snapshot_rows(controller, self.tickers)
        text = _format_watch_table(
            rows,
            event_count=event_count,
            updated_ticker=updated_ticker,
            final=final,
        )
        prefix = "\x1b[2J\x1b[H" if sys.stdout.isatty() else ""

        print(prefix + text, end="", flush=True)


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


def _best_level(book: Orderbook | None, side: str) -> tuple[str, str]:
    if book is None:
        return "-", "-"

    if side == "bid":
        price = book.best_bid
        levels = book.bids
    else:
        price = book.best_ask
        levels = book.asks

    if price is None:
        return "-", "-"

    return format_price_fp(price), format_count_fp(levels[price])


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
    args = parser.parse_args()

    if args.speed is not None and args.speed < 0:
        parser.error("--speed must be non-negative")

    if args.watch_interval <= 0:
        parser.error("--watch-interval must be greater than zero")

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

        raw_path = _latest_recording_dir(parser) if not raw_text else Path(raw_text)

    path = _resolve_recording_path(raw_path)

    if not (path / "manifest.json").exists():
        parser.error(f"recording manifest not found: {path / 'manifest.json'}")

    return path


def _resolve_recording_path(path: Path) -> Path:
    if path.is_absolute():
        return path

    cwd_path = Path.cwd() / path
    if (cwd_path / "manifest.json").exists():
        return cwd_path

    return ROOT / path


def _latest_recording_dir(parser: argparse.ArgumentParser) -> Path:
    recordings_dir = ROOT / "recordings"

    if not recordings_dir.exists():
        parser.error(f"recordings directory not found: {recordings_dir}")

    candidates = [
        path
        for path in recordings_dir.iterdir()
        if path.is_dir() and (path / "manifest.json").exists()
    ]

    if not candidates:
        parser.error(f"no recordings with manifest.json found under: {recordings_dir}")

    return max(candidates, key=lambda path: path.stat().st_mtime)


if __name__ == "__main__":
    main()
