from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from kalshi_mm_bot.recording.io import MANIFEST_FILE

RECORDINGS_DIR = "recordings"


def default_recording_dir(root: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return root / RECORDINGS_DIR / timestamp


def require_recording_path(
    raw_path: str | Path,
    *,
    root: Path,
    cwd: Path | None = None,
) -> Path:
    if isinstance(raw_path, str) and not raw_path.strip():
        raise ValueError("Choose a recording directory.")

    path = resolve_recording_path(Path(raw_path), root=root, cwd=cwd)
    manifest_path = path / MANIFEST_FILE

    if not manifest_path.exists():
        raise ValueError(f"Recording manifest not found: {manifest_path}")

    return path


def resolve_recording_path(
    path: Path,
    *,
    root: Path,
    cwd: Path | None = None,
) -> Path:
    if path.is_absolute():
        return path

    cwd_path = (cwd or Path.cwd()) / path
    if (cwd_path / MANIFEST_FILE).exists():
        return cwd_path

    return root / path


def latest_recording_dir(root: Path) -> Path:
    recordings_dir = root / RECORDINGS_DIR

    if not recordings_dir.exists():
        raise ValueError(f"Recordings directory not found: {recordings_dir}")

    candidates = [
        path
        for path in recordings_dir.iterdir()
        if path.is_dir() and (path / MANIFEST_FILE).exists()
    ]

    if not candidates:
        raise ValueError(f"No recordings with {MANIFEST_FILE} found under: {recordings_dir}")

    return max(candidates, key=lambda path: path.stat().st_mtime)
