from __future__ import annotations

import gzip
import json
import time
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any, TextIO

from kalshi_mm_bot.recording.schema import (
    RecordedEvent,
    RecordingManifest,
    utc_now_iso,
)


MANIFEST_FILE = "manifest.json"
EVENTS_FILE = "events.jsonl"
COMPRESSED_EVENTS_FILE = "events.jsonl.gz"


class RecordingSessionWriter:
    def __init__(
        self,
        directory: Path,
        event_path: Path,
        event_file: TextIO,
        *,
        flush_every: int = 1,
    ) -> None:
        if flush_every <= 0:
            raise ValueError("flush_every must be greater than zero")

        self.directory = directory
        self.event_path = event_path
        self.manifest_path = directory / MANIFEST_FILE
        self.started_at_utc = utc_now_iso()
        self.event_count = 0
        self.manifest: RecordingManifest | None = None

        self._event_file = event_file
        self._flush_every = flush_every
        self._started_at_monotonic = time.monotonic()
        self._closed = False

    @classmethod
    def create(
        cls,
        directory: str | Path,
        *,
        compress_events: bool = False,
        flush_every: int = 1,
    ) -> "RecordingSessionWriter":
        recording_dir = Path(directory)
        recording_dir.mkdir(parents=True, exist_ok=True)

        event_name = COMPRESSED_EVENTS_FILE if compress_events else EVENTS_FILE
        event_path = recording_dir / event_name
        manifest_path = recording_dir / MANIFEST_FILE

        if event_path.exists():
            raise FileExistsError(f"recording event file already exists: {event_path}")

        if manifest_path.exists():
            raise FileExistsError(f"recording manifest already exists: {manifest_path}")

        return cls(
            directory=recording_dir,
            event_path=event_path,
            event_file=_open_text(event_path, "wt"),
            flush_every=flush_every,
        )

    def write_event(self, msg: dict[str, Any]) -> None:
        if self._closed:
            raise RuntimeError("recording writer is closed")

        event = RecordedEvent(
            offset_seconds=time.monotonic() - self._started_at_monotonic,
            observed_at_utc=utc_now_iso(),
            msg=msg,
        )
        self._event_file.write(json.dumps(event.to_json_dict(), separators=(",", ":")))
        self._event_file.write("\n")
        self.event_count += 1

        if self.event_count % self._flush_every == 0:
            self._event_file.flush()

    def write_manifest(self, manifest: RecordingManifest) -> None:
        self.manifest = manifest
        _write_json_atomic(self.manifest_path, manifest.to_json_dict())

    def finalize(self) -> None:
        if self.manifest is None:
            return

        self.manifest = self.manifest.finalized(
            ended_at_utc=utc_now_iso(),
            event_count=self.event_count,
        )
        _write_json_atomic(self.manifest_path, self.manifest.to_json_dict())

    def close(self) -> None:
        if self._closed:
            return

        self._event_file.flush()
        self._event_file.close()
        self._closed = True

    def __enter__(self) -> "RecordingSessionWriter":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.finalize()
        self.close()


class RecordingSessionReader:
    def __init__(self, directory: Path, manifest: RecordingManifest) -> None:
        self.directory = directory
        self.manifest = manifest
        self.manifest_path = directory / MANIFEST_FILE
        self.events_path = directory / manifest.event_file

    @classmethod
    def open(cls, directory: str | Path) -> "RecordingSessionReader":
        recording_dir = Path(directory)
        manifest_path = recording_dir / MANIFEST_FILE

        with manifest_path.open("r", encoding="utf-8") as manifest_file:
            data = json.load(manifest_file)

        if not isinstance(data, dict):
            raise ValueError(f"manifest must be a JSON object: {manifest_path}")

        manifest = RecordingManifest.from_json_dict(data)
        reader = cls(recording_dir, manifest)

        if not reader.events_path.exists():
            raise FileNotFoundError(f"recording event file not found: {reader.events_path}")

        return reader

    def iter_events(self) -> Iterator[RecordedEvent]:
        return iter_recorded_events(self.events_path)


def iter_recorded_events(path: str | Path) -> Iterator[RecordedEvent]:
    with _open_text(Path(path), "rt") as event_file:
        for line_number, line in enumerate(event_file, start=1):
            line = line.strip()

            if not line:
                continue

            data = json.loads(line)

            if not isinstance(data, dict):
                raise ValueError(f"recorded event line {line_number} must be a JSON object")

            yield RecordedEvent.from_json_dict(data)


def _open_text(path: Path, mode: str) -> TextIO:
    if path.suffix == ".gz":
        return gzip.open(path, mode, encoding="utf-8", newline="")

    return path.open(mode, encoding="utf-8", newline="")


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")

    with temp_path.open("w", encoding="utf-8") as temp_file:
        json.dump(data, temp_file, indent=2, sort_keys=True)
        temp_file.write("\n")

    temp_path.replace(path)
