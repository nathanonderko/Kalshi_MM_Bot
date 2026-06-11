from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from kalshi_mm_bot.market.types import PriceRange


SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class RecordedEvent:
    offset_seconds: float
    observed_at_utc: str
    msg: dict[str, Any]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "offset_seconds": self.offset_seconds,
            "observed_at_utc": self.observed_at_utc,
            "msg": self.msg,
        }

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> "RecordedEvent":
        msg = data.get("msg")

        if not isinstance(msg, dict):
            raise ValueError("recorded event is missing JSON object field 'msg'")

        return cls(
            offset_seconds=float(data["offset_seconds"]),
            observed_at_utc=str(data["observed_at_utc"]),
            msg=msg,
        )


@dataclass(frozen=True, slots=True)
class RecordingManifest:
    schema_version: int
    environment: str
    tickers: tuple[str, ...]
    channels: tuple[str, ...]
    price_ranges_by_ticker: dict[str, tuple[PriceRange, ...]]
    event_file: str
    created_at_utc: str
    started_at_utc: str
    ended_at_utc: str | None = None
    event_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        environment: str,
        tickers: tuple[str, ...],
        channels: tuple[str, ...],
        price_ranges_by_ticker: dict[str, tuple[PriceRange, ...]],
        event_file: str,
        started_at_utc: str,
        metadata: dict[str, Any] | None = None,
    ) -> "RecordingManifest":
        return cls(
            schema_version=SCHEMA_VERSION,
            environment=environment,
            tickers=tickers,
            channels=channels,
            price_ranges_by_ticker={
                ticker: tuple(ranges)
                for ticker, ranges in price_ranges_by_ticker.items()
            },
            event_file=event_file,
            created_at_utc=utc_now_iso(),
            started_at_utc=started_at_utc,
            metadata={} if metadata is None else dict(metadata),
        )

    def finalized(self, *, ended_at_utc: str, event_count: int) -> "RecordingManifest":
        return replace(self, ended_at_utc=ended_at_utc, event_count=event_count)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "environment": self.environment,
            "tickers": list(self.tickers),
            "channels": list(self.channels),
            "price_ranges_by_ticker": {
                ticker: [
                    {"start": price_range.start, "end": price_range.end, "step": price_range.step}
                    for price_range in ranges
                ]
                for ticker, ranges in self.price_ranges_by_ticker.items()
            },
            "event_file": self.event_file,
            "created_at_utc": self.created_at_utc,
            "started_at_utc": self.started_at_utc,
            "ended_at_utc": self.ended_at_utc,
            "event_count": self.event_count,
            "metadata": self.metadata,
        }

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> "RecordingManifest":
        schema_version = int(data["schema_version"])

        if schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported recording schema {schema_version}; expected {SCHEMA_VERSION}"
            )

        raw_price_ranges = data["price_ranges_by_ticker"]

        if not isinstance(raw_price_ranges, dict):
            raise ValueError("manifest field 'price_ranges_by_ticker' must be an object")

        return cls(
            schema_version=schema_version,
            environment=str(data["environment"]),
            tickers=_string_tuple(data["tickers"]),
            channels=_string_tuple(data["channels"]),
            price_ranges_by_ticker={
                str(ticker): tuple(
                    PriceRange(
                        start=int(price_range["start"]),
                        end=int(price_range["end"]),
                        step=int(price_range["step"]),
                    )
                    for price_range in ranges
                )
                for ticker, ranges in raw_price_ranges.items()
            },
            event_file=str(data["event_file"]),
            created_at_utc=str(data["created_at_utc"]),
            started_at_utc=str(data["started_at_utc"]),
            ended_at_utc=(
                None if data.get("ended_at_utc") is None else str(data["ended_at_utc"])
            ),
            event_count=int(data.get("event_count", 0)),
            metadata=dict(data.get("metadata", {})),
        )


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError("expected a JSON array of strings")

    return tuple(str(item) for item in value)
