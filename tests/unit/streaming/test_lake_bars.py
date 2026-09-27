"""The ``lake_bars`` source: lake bars replayed as stream events (roadmap 21.2.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from stonks.config import Settings
from stonks.core.clock import FakeClock
from stonks.core.interval import Interval
from stonks.core.stream import StreamBar
from stonks.streaming.base import StreamConfigError, StreamContext
from stonks.streaming.registry import stream_source_classes
from stonks.streaming.sources.lake_bars import LakeBarSource
from stonks.streaming.writer import BarWriter

M0 = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
ONE = timedelta(minutes=1)


def bar(minute: int, ticker: str = "A.US", close: float = 10.0) -> StreamBar:
    return StreamBar(
        ticker, M0 + minute * ONE, Interval.MIN_1, close, close + 1, close - 1, close, 7
    )


class FakeReader:
    """A bar reader over canned frames that records every request."""

    def __init__(self, bars: list[StreamBar]) -> None:
        self.bars = bars
        self.calls: list[tuple[str, Interval, datetime, datetime]] = []

    def get_bars(self, ticker, interval, start, end) -> pd.DataFrame:
        self.calls.append((ticker, interval, start, end))
        rows = [
            {
                "ticker": b.ticker,
                "timestamp": b.timestamp.replace(tzinfo=None),
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "adj_close": b.close,
                "volume": b.volume,
            }
            for b in self.bars
            if b.ticker == ticker
            and b.interval == interval
            and start <= b.timestamp.replace(tzinfo=None) <= end
        ]
        return pd.DataFrame(
            rows,
            columns=["ticker", "timestamp", "open", "high", "low", "close", "adj_close", "volume"],
        )


def test_registered_and_finite():
    assert stream_source_classes()["lake_bars"] is LakeBarSource
    assert LakeBarSource.finite


def test_from_settings_is_a_config_error():
    with pytest.raises(StreamConfigError, match="lake_bars"):
        LakeBarSource.from_settings(StreamContext(settings=Settings()))


def test_yields_bars_in_time_then_ticker_order():
    reader = FakeReader([bar(1, "B.US"), bar(0, "B.US"), bar(1, "A.US"), bar(0, "A.US")])
    src = LakeBarSource(reader, start=M0, end=M0 + 10 * ONE)
    got = [(e.timestamp, e.ticker) for e in src.stream(["B.US", "A.US"])]
    assert got == [
        (M0, "A.US"),
        (M0, "B.US"),
        (M0 + ONE, "A.US"),
        (M0 + ONE, "B.US"),
    ]


def test_bars_carry_the_lake_values():
    src = LakeBarSource(FakeReader([bar(0, close=12.5)]), start=M0, end=M0 + ONE)
    (event,) = list(src.stream(["A.US"]))
    assert isinstance(event, StreamBar)
    assert (event.open, event.high, event.low, event.close, event.volume) == (
        12.5,
        13.5,
        11.5,
        12.5,
        7,
    )
    assert event.interval == Interval.MIN_1
    assert event.source == "lake"
    assert event.timestamp.tzinfo is UTC


def test_only_bars_closed_by_the_end_are_yielded():
    # the bar starting at minute 5 closes at minute 6, after the end
    reader = FakeReader([bar(m) for m in range(8)])
    src = LakeBarSource(reader, start=M0, end=M0 + 5 * ONE + timedelta(seconds=30))
    starts = [e.timestamp for e in src.stream(["A.US"])]
    assert starts == [M0 + m * ONE for m in range(5)]


def test_bars_starting_before_the_start_are_skipped():
    reader = FakeReader([bar(m) for m in range(4)])
    src = LakeBarSource(reader, start=M0 + 2 * ONE, end=M0 + 10 * ONE)
    assert [e.timestamp for e in src.stream(["A.US"])] == [M0 + 2 * ONE, M0 + 3 * ONE]


def test_the_clock_reads_each_bar_close_so_no_bar_is_seen_early():
    reader = FakeReader([bar(m, t) for m in range(3) for t in ("A.US", "B.US")])
    clock = FakeClock(M0 - ONE)
    src = LakeBarSource(reader, start=M0, end=M0 + 3 * ONE, clock=clock)
    seen: list[StreamBar] = []
    for event in src.stream(["A.US", "B.US"]):
        assert isinstance(event, StreamBar)
        # point in time: every bar handed out so far had closed by now
        seen.append(event)
        assert all(b.timestamp + ONE <= clock.now() for b in seen)
        assert clock.now() == event.timestamp + ONE


def test_reads_in_chunks_that_never_reach_past_the_end():
    reader = FakeReader([bar(m) for m in range(0, 60 * 30, 60)])  # hourly, 30 hours
    end = M0 + 30 * 60 * ONE
    src = LakeBarSource(reader, start=M0, end=end, chunk=timedelta(hours=6))
    events = list(src.stream(["A.US"]))
    assert len(events) == 30
    assert len(reader.calls) == 5
    assert all(call_end.replace(tzinfo=UTC) <= end for *_, call_end in reader.calls)


def test_bad_rows_are_skipped_and_counted():
    reader = FakeReader([bar(0), bar(1)])
    good = reader.get_bars

    def with_bad_row(ticker, interval, start, end):
        frame = good(ticker, interval, start, end)
        if len(frame):
            frame.loc[0, "high"] = 1.0  # high below close
        return frame

    reader.get_bars = with_bad_row  # type: ignore[method-assign]
    src = LakeBarSource(reader, start=M0, end=M0 + 5 * ONE)
    assert [e.timestamp for e in src.stream(["A.US"])] == [M0 + ONE]
    assert src.skipped_rows == 1


def test_close_stops_the_stream():
    src = LakeBarSource(FakeReader([bar(m) for m in range(5)]), start=M0, end=M0 + 9 * ONE)
    it = src.stream(["A.US"])
    next(it)
    src.close()
    assert list(it) == []


def test_refuses_bad_arguments():
    reader = FakeReader([])
    with pytest.raises(ValueError, match="timezone"):
        LakeBarSource(reader, start=datetime(2026, 1, 1), end=M0)
    with pytest.raises(ValueError, match="before"):
        LakeBarSource(reader, start=M0, end=M0)
    with pytest.raises(ValueError, match="intraday"):
        LakeBarSource(reader, start=M0, end=M0 + ONE, interval=Interval.DAY_1)
    with pytest.raises(ValueError, match="ticker"):
        list(LakeBarSource(reader, start=M0, end=M0 + ONE).stream([]))


def test_replays_bars_from_a_real_lake(lake):
    w = BarWriter(lake.bar_store)
    w.add([bar(m, t, 10.0 + m) for m in range(3) for t in ("A.US", "B.US")])
    w.flush()
    src = LakeBarSource(lake, start=M0, end=M0 + 3 * ONE)
    events = list(src.stream(["A.US", "B.US"]))
    assert [(e.ticker, e.close) for e in events if isinstance(e, StreamBar)] == [
        ("A.US", 10.0),
        ("B.US", 10.0),
        ("A.US", 11.0),
        ("B.US", 11.0),
        ("A.US", 12.0),
        ("B.US", 12.0),
    ]
