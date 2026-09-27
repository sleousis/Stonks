"""Streamed bars into the BarStore (roadmap 21.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from stonks.core.interval import Interval
from stonks.core.stream import StreamBar
from stonks.store.bars import BarStore, ParquetBarStore
from stonks.streaming.writer import BarWriter

M0 = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
LO, HI = datetime(2026, 1, 1), datetime(2027, 1, 1)


def bar(minute: int, close: float = 10.0, ticker: str = "A.US") -> StreamBar:
    return StreamBar(
        ticker,
        M0 + timedelta(minutes=minute),
        Interval.MIN_1,
        close,
        close + 1,
        close - 1,
        close,
        5,
    )


def test_flush_writes_bars_with_the_bars_columns(lake):
    w = BarWriter(lake.bar_store)
    w.add([bar(0), bar(1, 11.0)])
    assert w.pending == 2
    assert w.flush() == 2
    got = lake.get_bars("A.US", Interval.MIN_1, LO, HI)
    assert list(got["close"]) == [10.0, 11.0]
    assert list(got["adj_close"]) == [10.0, 11.0]
    assert list(got["volume"]) == [5, 5]
    assert got["timestamp"].iloc[0] == M0.replace(tzinfo=None)
    assert w.bars_written == 2 and w.pending == 0


def test_rewriting_the_same_bars_is_idempotent(lake):
    w = BarWriter(lake.bar_store)
    w.add([bar(0), bar(1)])
    w.flush()
    w.add([bar(0, 12.0)])
    w.flush()
    got = lake.get_bars("A.US", Interval.MIN_1, LO, HI)
    assert len(got) == 2
    assert got["close"].iloc[0] == 12.0


def test_maybe_flush_by_count_and_by_time(lake):
    w = BarWriter(lake.bar_store, flush_bars=3, flush_every=timedelta(seconds=10))
    w.add([bar(0)])
    assert w.maybe_flush(M0) == 0  # first call starts the timer
    w.add([bar(1), bar(2)])
    assert w.maybe_flush(M0 + timedelta(seconds=1)) == 3
    w.add([bar(3)])
    assert w.maybe_flush(M0 + timedelta(seconds=5)) == 0
    assert w.maybe_flush(M0 + timedelta(seconds=12)) == 1


def test_bars_of_another_interval_are_refused(lake):
    w = BarWriter(lake.bar_store)
    other = StreamBar("A.US", M0, Interval.MIN_5, 1.0, 1.0, 1.0, 1.0, 0)
    with pytest.raises(ValueError, match="5m"):
        w.add([other])


class _Broken(BarStore):
    backend = "duckdb"

    def __init__(self) -> None:
        self.fail = True
        self.frames: list[int] = []

    def upsert(self, frame):
        if self.fail:
            raise OSError("disk full")
        self.frames.append(len(frame))
        return len(frame)

    def get(self, ticker, interval, start, end):  # pragma: no cover - unused
        raise NotImplementedError

    def delete(self, ticker, interval, timestamps):  # pragma: no cover - unused
        raise NotImplementedError

    def aggregate(self, ticker, source, target):  # pragma: no cover - unused
        raise NotImplementedError

    def series_checksums(self):  # pragma: no cover - unused
        raise NotImplementedError


def test_a_failed_flush_keeps_the_bars_for_the_next_one():
    store = _Broken()
    w = BarWriter(store)
    w.add([bar(0), bar(1)])
    with pytest.raises(OSError):
        w.flush()
    assert w.pending == 2 and w.failed_flushes == 1
    store.fail = False
    assert w.flush() == 2
    assert store.frames == [2]


def test_writes_to_the_parquet_store(tmp_path):
    con = duckdb.connect()
    try:
        store = ParquetBarStore(tmp_path / "bars", con)
        store.ensure_layout()
        w = BarWriter(store)
        w.add([bar(0), bar(0, 13.0)])  # last write wins inside one batch too
        w.flush()
        got = store.get("A.US", Interval.MIN_1, LO, HI)
        assert list(got["close"]) == [13.0]
    finally:
        con.close()
