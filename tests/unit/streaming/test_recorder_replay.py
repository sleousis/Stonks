"""Record a stream to Parquet and play it back (roadmap 21.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stonks.config import Settings
from stonks.core.clock import FakeClock
from stonks.core.interval import Interval
from stonks.core.stream import Heartbeat, QuoteTick, StreamBar, TradeTick
from stonks.streaming.base import StreamContext
from stonks.streaming.recorder import StreamRecorder, read_recording
from stonks.streaming.registry import build_stream_source, stream_source_classes
from stonks.streaming.sources.replay import ReplaySource

T0 = datetime(2026, 9, 28, 13, 30, 0, 123_000, tzinfo=UTC)
M29 = datetime(2026, 9, 28, 13, 29, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


EVENTS = [
    StreamBar("MSFT.US", M29, Interval.MIN_1, 400.0, 401.0, 399.0, 400.5, 1200, source="v"),
    TradeTick("AAPL.US", at(0), 227.31, 100, source="eodhd"),
    QuoteTick("AAPL.US", at(0.5), bid=227.3, ask=227.33, bid_size=2, ask_size=3, source="eodhd"),
    QuoteTick("MSFT.US", at(1), bid=None, ask=400.0, last=399.9, delayed=True, source="ibkr"),
    Heartbeat(at(5), source="eodhd"),
    TradeTick("BTC-USD.CC", at(6), 60_000.5, 0.25, source="eodhd"),
]


def test_round_trip_keeps_every_event_kind(tmp_path):
    with StreamRecorder(tmp_path) as rec:
        for ev in EVENTS:
            rec.write(ev)
    assert rec.events_written == len(EVENTS)
    assert len(rec.files) == 1
    assert rec.files[0].parent.name == "2026-09-28"
    assert list(read_recording(tmp_path)) == EVENTS


def test_chunks_by_count_and_reads_back_in_time_order(tmp_path):
    rec = StreamRecorder(tmp_path, chunk_events=2)
    for ev in reversed(EVENTS):  # written out of order
        rec.write(ev)
    rec.close()
    assert len(rec.files) == 3
    got = list(read_recording(tmp_path))
    assert [e.timestamp for e in got] == sorted(e.timestamp for e in EVENTS)
    key = lambda e: (e.timestamp, e.kind)  # noqa: E731
    assert sorted(got, key=key) == sorted(EVENTS, key=key)


def test_chunks_by_time(tmp_path):
    clock = FakeClock(T0)
    rec = StreamRecorder(tmp_path, chunk_every=timedelta(seconds=30), clock=clock)
    rec.write(EVENTS[0])
    clock.advance(timedelta(seconds=31))
    rec.write(EVENTS[1])
    assert len(rec.files) == 1  # the second write flushed both
    rec.close()
    assert rec.files and len(rec.files) == 1


def test_read_filters_tickers_and_window_but_keeps_heartbeats(tmp_path):
    with StreamRecorder(tmp_path) as rec:
        for ev in EVENTS:
            rec.write(ev)
    got = list(read_recording(tmp_path, tickers=["AAPL.US"]))
    assert [type(e).__name__ for e in got] == ["TradeTick", "QuoteTick", "Heartbeat"]
    got = list(read_recording(tmp_path, start=at(1), end=at(5)))
    assert [e.timestamp for e in got] == [at(1), at(5)]


def test_empty_or_missing_recordings_read_as_nothing(tmp_path):
    assert list(read_recording(tmp_path / "none")) == []
    StreamRecorder(tmp_path).close()
    assert list(read_recording(tmp_path)) == []


def test_replay_is_registered_and_finite():
    assert "replay" in stream_source_classes()
    assert ReplaySource.finite


def test_replay_yields_the_recording_and_drives_a_fake_clock(tmp_path):
    with StreamRecorder(tmp_path) as rec:
        for ev in EVENTS:
            rec.write(ev)
    clock = FakeClock(datetime(2020, 1, 1, tzinfo=UTC))
    seen = []
    src = ReplaySource(tmp_path, clock=clock)
    for ev in src.stream([]):
        assert clock.now() == ev.timestamp
        seen.append(ev)
    assert seen == EVENTS


def test_replay_paces_by_speed(tmp_path):
    with StreamRecorder(tmp_path) as rec:
        rec.write(EVENTS[1])
        rec.write(EVENTS[4])  # 5 s later
    waits: list[float] = []
    src = ReplaySource(tmp_path, speed=10.0, sleep=waits.append)
    list(src.stream([]))
    assert waits == [pytest.approx(0.5, abs=1e-3)]


def test_replay_close_stops_early(tmp_path):
    with StreamRecorder(tmp_path) as rec:
        for ev in EVENTS:
            rec.write(ev)
    src = ReplaySource(tmp_path)
    it = src.stream([])
    next(it)
    src.close()
    assert list(it) == []


def test_replay_builds_from_settings(tmp_path):
    settings = Settings()
    settings.streaming.replay.path = str(tmp_path)
    settings.streaming.replay.speed = 2.0
    clock = FakeClock(T0)
    src = build_stream_source(StreamContext(settings=settings, clock=clock), "replay")
    assert isinstance(src, ReplaySource)
    assert src.speed == 2.0 and src.clock is clock
