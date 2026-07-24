import pytest

from kalshi_mm_bot.market.tickers import parse_ticker_tuple
from kalshi_mm_bot.recording.io import MANIFEST_FILE
from kalshi_mm_bot.recording.paths import require_recording_path


def test_parse_ticker_tuple_normalizes_commas_spaces_and_duplicates() -> None:
    assert parse_ticker_tuple([" m1,m2 ", "M1  m3"]) == ("M1", "M2", "M3")


def test_require_recording_path_rejects_blank_text(tmp_path) -> None:
    with pytest.raises(ValueError, match="Choose a recording directory"):
        require_recording_path("", root=tmp_path)


def test_require_recording_path_prefers_cwd_match(tmp_path) -> None:
    root = tmp_path / "root"
    cwd = tmp_path / "cwd"
    recording = cwd / "session"
    recording.mkdir(parents=True)
    (recording / MANIFEST_FILE).write_text("{}", encoding="utf-8")

    assert require_recording_path("session", root=root, cwd=cwd) == recording
