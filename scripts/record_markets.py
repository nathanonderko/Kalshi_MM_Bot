from __future__ import annotations

import argparse
import asyncio
import sys
from contextlib import suppress
from pathlib import Path
from typing import cast

from websockets.exceptions import InvalidStatus

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kalshi_mm_bot.api.auth import KalshiAuth
from kalshi_mm_bot.api.feed_controller import (
    DEFAULT_FEED_CHANNELS,
    FEED_CHANNELS,
    FeedChannel,
    FeedController,
)
from kalshi_mm_bot.api.rest import KalshiRestClient
from kalshi_mm_bot.api.websocket import KalshiWebSocketClient
from kalshi_mm_bot.config import load_settings
from kalshi_mm_bot.market.tickers import parse_ticker_tuple
from kalshi_mm_bot.recording import RecordingManifest, RecordingSessionWriter
from kalshi_mm_bot.recording.clients import RecordingWebSocketClient
from kalshi_mm_bot.recording.paths import default_recording_dir


def main() -> None:
    args = _parse_args()

    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("Recording stopped.")


async def _run(args: argparse.Namespace) -> None:
    tickers = args.tickers
    channels = cast(tuple[FeedChannel, ...], tuple(dict.fromkeys(args.channels)))
    output_dir = args.output or default_recording_dir(ROOT)

    settings = load_settings()
    environment = settings.environment(prod=args.prod)
    auth = KalshiAuth(settings.api_key_id, settings.private_key_path)

    writer = RecordingSessionWriter.create(
        output_dir,
        compress_events=args.compress,
        flush_every=args.flush_every,
    )
    rest = KalshiRestClient(environment.rest_base_url, auth)
    ws = RecordingWebSocketClient(
        KalshiWebSocketClient(environment.ws_url, auth),
        writer,
    )
    controller = FeedController(rest=rest, ws=ws)
    receiver: asyncio.Task[None] | None = None

    try:
        print(f"Connecting to {environment.name}...")
        await controller.connect()
        print(f"Subscribing to {len(tickers)} market(s): {', '.join(tickers)}")
        await controller.subscribe(tickers, channels=channels)

        writer.write_manifest(
            RecordingManifest.create(
                environment=environment.name,
                tickers=tickers,
                channels=channels,
                price_ranges_by_ticker=controller.price_ranges_by_ticker,
                event_file=writer.event_path.name,
                started_at_utc=writer.started_at_utc,
                metadata={
                    "rest_base_url": environment.rest_base_url,
                    "ws_url": environment.ws_url,
                },
            )
        )

        print(f"Recording to {writer.directory}")
        receiver = asyncio.create_task(controller.run_forever())
        await _wait_for_stop(receiver, args.duration_sec)
    except InvalidStatus as error:
        if getattr(error.response, "status_code", None) == 401:
            raise RuntimeError(
                f"Kalshi rejected websocket auth for {environment.name} (HTTP 401). "
                "Use production keys with --prod, or demo keys with --demo."
            ) from error

        raise
    finally:
        try:
            if receiver is not None:
                receiver.cancel()
                with suppress(asyncio.CancelledError):
                    await receiver
        finally:
            await controller.close()
            writer.finalize()
            writer.close()
            print(f"Wrote {writer.event_count} event(s) to {writer.event_path}")


async def _wait_for_stop(receiver: asyncio.Task[None], duration_sec: float | None) -> None:
    if duration_sec is None:
        await receiver
        return

    try:
        await asyncio.wait_for(asyncio.shield(receiver), timeout=duration_sec)
    except asyncio.TimeoutError:
        return


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record Kalshi websocket market data.")
    parser.add_argument(
        "tickers",
        nargs="*",
        help="Market tickers to record. If omitted, you will be prompted.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Recording directory. Defaults to recordings/<UTC timestamp>.",
    )
    parser.add_argument(
        "--channels",
        nargs="+",
        choices=FEED_CHANNELS,
        default=list(DEFAULT_FEED_CHANNELS),
        help="Feed channels to record.",
    )
    parser.add_argument(
        "--duration-sec",
        type=float,
        help="Stop automatically after this many seconds.",
    )
    parser.add_argument(
        "--compress",
        action="store_true",
        help="Write events as gzip-compressed JSONL.",
    )
    parser.add_argument(
        "--flush-every",
        type=int,
        default=1,
        help="Flush the event file every N messages. Default: 1.",
    )

    environment = parser.add_mutually_exclusive_group()
    environment.add_argument(
        "--prod",
        dest="prod",
        action="store_true",
        default=True,
        help="Use production Kalshi endpoints. Default.",
    )
    environment.add_argument(
        "--demo",
        dest="prod",
        action="store_false",
        help="Use demo Kalshi endpoints.",
    )

    args = parser.parse_args()
    args.tickers = _ticker_tuple(args.tickers, parser)

    if args.duration_sec is not None and args.duration_sec <= 0:
        parser.error("--duration-sec must be greater than zero")

    if args.flush_every <= 0:
        parser.error("--flush-every must be greater than zero")

    return args


def _ticker_tuple(raw_tickers: list[str], parser: argparse.ArgumentParser) -> tuple[str, ...]:
    if not raw_tickers:
        try:
            raw_text = input("Market tickers (space or comma separated): ")
        except EOFError:
            parser.error("provide at least one market ticker")

        raw_tickers = [raw_text]

    ticker_tuple = parse_ticker_tuple(raw_tickers)

    if not ticker_tuple:
        parser.error("provide at least one market ticker")

    return ticker_tuple


if __name__ == "__main__":
    main()
