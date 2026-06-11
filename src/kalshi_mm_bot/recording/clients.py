from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any

from kalshi_mm_bot.market.types import PriceRange
from kalshi_mm_bot.recording.io import RecordingSessionReader, iter_recorded_events
from kalshi_mm_bot.recording.schema import RecordedEvent, RecordingManifest


class RecordingWebSocketClient:
    def __init__(self, inner: Any, writer: Any) -> None:
        self.inner = inner
        self.writer = writer

    async def connect(self) -> None:
        await self.inner.connect()

    async def close(self) -> None:
        await self.inner.close()

    async def send_json(self, payload: dict[str, Any]) -> None:
        await self.inner.send_json(payload)

    async def recv_json(self) -> dict[str, Any]:
        msg = await self.inner.recv_json()
        self.writer.write_event(msg)
        return msg


class RecordedRestClient:
    def __init__(self, manifest: RecordingManifest) -> None:
        self.manifest = manifest
        self.closed = False

    async def get_market_price_ranges(
        self,
        tickers: list[str] | tuple[str, ...],
    ) -> dict[str, tuple[PriceRange, ...]]:
        missing = [
            ticker
            for ticker in tickers
            if ticker not in self.manifest.price_ranges_by_ticker
        ]

        if missing:
            raise KeyError(
                "recording has no price ranges for: " + ", ".join(missing)
            )

        return {
            ticker: self.manifest.price_ranges_by_ticker[ticker]
            for ticker in tickers
        }

    async def close(self) -> None:
        self.closed = True


class RecordedWebSocketClient:
    def __init__(
        self,
        events_path: str | Path,
        *,
        speed_multiplier: float = 0.0,
    ) -> None:
        if speed_multiplier < 0:
            raise ValueError("speed_multiplier must be non-negative")

        self.events_path = Path(events_path)
        self.speed_multiplier = speed_multiplier
        self.sent: list[dict[str, Any]] = []
        self.returned_count = 0

        self._events: Iterator[RecordedEvent] | None = None
        self._connected = False
        self._replay_start_monotonic: float | None = None

    @classmethod
    def from_session(
        cls,
        reader: RecordingSessionReader,
        *,
        speed_multiplier: float = 0.0,
    ) -> "RecordedWebSocketClient":
        return cls(reader.events_path, speed_multiplier=speed_multiplier)

    async def connect(self) -> None:
        if self._connected:
            raise RuntimeError("recorded websocket is already connected")

        self._events = iter_recorded_events(self.events_path)
        self._connected = True
        self._replay_start_monotonic = None
        self.returned_count = 0
        self.sent.clear()

    async def close(self) -> None:
        self._events = None
        self._connected = False
        self._replay_start_monotonic = None

    async def send_json(self, payload: dict[str, Any]) -> None:
        self._require_connected()
        self.sent.append(deepcopy(payload))

    async def recv_json(self) -> dict[str, Any]:
        self._require_connected()

        assert self._events is not None

        try:
            event = next(self._events)
        except StopIteration as error:
            raise EOFError("recording is exhausted") from error

        await self._sleep_until(event)
        self.returned_count += 1
        return deepcopy(event.msg)

    async def _sleep_until(self, event: RecordedEvent) -> None:
        if self.speed_multiplier <= 0:
            return

        now = time.monotonic()

        if self._replay_start_monotonic is None:
            self._replay_start_monotonic = now - event.offset_seconds / self.speed_multiplier

        target = self._replay_start_monotonic + event.offset_seconds / self.speed_multiplier
        delay = target - time.monotonic()

        if delay > 0:
            await asyncio.sleep(delay)

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("recorded websocket is not connected")
