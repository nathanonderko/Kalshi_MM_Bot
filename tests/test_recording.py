import asyncio

from kalshi_mm_bot.api.feed_controller import FeedController, ORDERBOOK_CHANNEL
from kalshi_mm_bot.market.types import PriceRange
from kalshi_mm_bot.recording import (
    RecordedRestClient,
    RecordedWebSocketClient,
    RecordingManifest,
    RecordingSessionReader,
    RecordingSessionWriter,
)


PRICE_RANGES = {"M1": (PriceRange(start=0, end=10000, step=10),)}


def subscribed(command_id: int = 1, channel: str = ORDERBOOK_CHANNEL, sid: int = 10) -> dict:
    return {
        "id": command_id,
        "type": "subscribed",
        "msg": {"channel": channel, "sid": sid},
    }


def snapshot(seq: int, ticker: str, bid: str = "0.5000") -> dict:
    return {
        "type": "orderbook_snapshot",
        "sid": 10,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": [[bid, "1.00"]],
            "no_dollars_fp": [],
        },
    }


def delta(seq: int, ticker: str, price: str, amount: str) -> dict:
    return {
        "type": "orderbook_delta",
        "sid": 10,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "side": "yes",
            "price_dollars": price,
            "delta_fp": amount,
        },
    }


def test_recording_writer_and_reader_round_trip_manifest_and_events(tmp_path) -> None:
    recording_dir = tmp_path / "session"

    with RecordingSessionWriter.create(recording_dir) as writer:
        writer.write_event(subscribed())
        writer.write_manifest(
            RecordingManifest.create(
                environment="demo",
                tickers=("M1",),
                channels=(ORDERBOOK_CHANNEL,),
                price_ranges_by_ticker=PRICE_RANGES,
                event_file=writer.event_path.name,
                started_at_utc=writer.started_at_utc,
            )
        )

    reader = RecordingSessionReader.open(recording_dir)
    events = tuple(reader.iter_events())

    assert reader.manifest.environment == "demo"
    assert reader.manifest.tickers == ("M1",)
    assert reader.manifest.channels == (ORDERBOOK_CHANNEL,)
    assert reader.manifest.price_ranges_by_ticker == PRICE_RANGES
    assert reader.manifest.event_count == 1
    assert reader.manifest.ended_at_utc is not None
    assert events[0].msg == subscribed()


def test_recorded_clients_replay_through_feed_controller(tmp_path) -> None:
    recording_dir = tmp_path / "session"

    with RecordingSessionWriter.create(recording_dir) as writer:
        writer.write_event(subscribed())
        writer.write_event(snapshot(1, "M1", "0.5000"))
        writer.write_event(delta(2, "M1", "0.6000", "2.00"))
        writer.write_manifest(
            RecordingManifest.create(
                environment="demo",
                tickers=("M1",),
                channels=(ORDERBOOK_CHANNEL,),
                price_ranges_by_ticker=PRICE_RANGES,
                event_file=writer.event_path.name,
                started_at_utc=writer.started_at_utc,
            )
        )

    async def run_replay() -> None:
        reader = RecordingSessionReader.open(recording_dir)
        ws = RecordedWebSocketClient.from_session(reader)
        rest = RecordedRestClient(reader.manifest)
        controller = FeedController(rest=rest, ws=ws)

        await controller.connect()
        await controller.subscribe(("M1",), channels=(ORDERBOOK_CHANNEL,))
        assert await controller.recv() == "M1"
        assert await controller.recv() == "M1"

        book = controller.orderbooks["M1"]
        assert book.best_bid == 6000
        assert book.bids[6000] == 200
        assert ws.returned_count == 3

        await controller.close()

    asyncio.run(run_replay())
