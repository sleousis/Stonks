"""Engine monitoring: latency histograms, the monitor handler and its status
row (roadmap 21.3.4)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Self

import pytest

from stonks.core.clock import FakeClock
from stonks.core.interval import Interval
from stonks.core.stream import StreamBar, StreamEvent
from stonks.engine.driver import BarClose, EventDriver
from stonks.engine.monitor import LATENCY_BOUNDS, EngineMonitor, LatencyHistogram
from stonks.engine.status import EngineStatusStore
from stonks.store.state import SqliteState
from stonks.streaming.base import StreamContext, StreamingSource
from stonks.streaming.health import StreamHealth

M0 = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
ONE = timedelta(minutes=1)


def bar(minute: int, ticker: str = "A.US") -> StreamBar:
    return StreamBar(ticker, M0 + minute * ONE, Interval.MIN_1, 10, 11, 9, 10, 3)


class ListSource(StreamingSource):
    finite = True

    def __init__(self, events: Sequence[StreamEvent]) -> None:
        self.events = list(events)

    @classmethod
    def from_settings(cls, ctx: StreamContext) -> Self:
        raise NotImplementedError

    def stream(self, tickers: Sequence[str]) -> Iterator[StreamEvent]:
        yield from self.events

    def close(self) -> None:
        return None


@pytest.fixture
def state_path(tmp_path):
    path = tmp_path / "state.sqlite"
    with SqliteState(path) as s:
        s.migrate()
    return path


# ---- LatencyHistogram --------------------------------------------------------------


def test_histogram_counts_observations_into_buckets():
    h = LatencyHistogram(bounds=(0.1, 1.0))
    for v in (0.05, 0.1, 0.5, 3.0, -1.0):
        h.observe(v)
    # a negative reading (clock skew) counts as zero
    assert h.counts == [3, 1, 1]
    assert h.count == 5
    assert h.total == pytest.approx(3.65)
    assert h.max == 3.0


def test_histogram_quantiles_use_bucket_upper_bounds():
    h = LatencyHistogram(bounds=(0.1, 1.0))
    assert h.quantile(0.5) is None
    for v in (0.05, 0.05, 0.5, 5.0):
        h.observe(v)
    assert h.quantile(0.5) == 0.1
    assert h.quantile(0.75) == 1.0
    # the +Inf bucket reports the largest reading seen
    assert h.quantile(0.99) == 5.0


def test_histogram_round_trips_through_a_dict():
    h = LatencyHistogram()
    h.observe(0.2)
    h.observe(12)
    back = LatencyHistogram.from_dict(h.as_dict())
    assert back == h
    assert back.bounds == LATENCY_BOUNDS


def test_histogram_from_a_bad_dict_is_empty():
    assert LatencyHistogram.from_dict({"bounds": [1], "counts": [1, 2, 3]}).count == 0
    assert LatencyHistogram.from_dict(None).count == 0


# ---- EngineMonitor -----------------------------------------------------------------


def make(events, *, settle=timedelta(seconds=2), health=None, store=None, **kw):
    clock = FakeClock(M0 - ONE)
    driver = EventDriver(ListSource(events), clock=clock, settle=settle)
    monitor = EngineMonitor(driver, engine_id="e1", health=health, store=store, **kw)
    monitor.attach()
    return driver, monitor, clock


def test_monitor_runs_first_and_publishes_last():
    driver, _, _ = make([])
    driver.register(lambda e: None, name="strategy")
    names = driver.handler_names()
    assert names[0] == "monitor.dispatch"
    assert names[-1] == "monitor.publish"


def test_monitor_records_dispatches_and_lag():
    driver, monitor, _ = make([bar(0), bar(1), bar(2)])
    driver.run(["A.US"])
    assert monitor.dispatch_lag.count == 3
    # replay decides at close plus settle, so the lag is zero
    assert monitor.dispatch_lag.max == 0
    assert monitor.last_dispatch_at == M0 + 3 * ONE + timedelta(seconds=2)


def test_live_lag_is_clock_time_past_the_settle_time():
    driver, monitor, clock = make([])
    close = BarClose(at=M0, interval=Interval.MIN_1, bars=(bar(-1),), sequence=1)
    clock.set(M0 + timedelta(seconds=5))
    monitor.on_dispatch(close)
    assert monitor.dispatch_lag.total == pytest.approx(3.0)


def test_event_to_order_latency_starts_at_the_dispatch():
    driver, monitor, clock = make([])
    close = BarClose(at=M0, interval=Interval.MIN_1, bars=(bar(-1),), sequence=7)
    clock.set(M0 + timedelta(seconds=2))
    monitor.on_dispatch(close)
    latency = monitor.record_order(close, submitted_at=M0 + timedelta(seconds=2.5))
    assert latency == pytest.approx(0.5)
    assert monitor.event_to_order.count == 1


def test_event_to_order_for_an_unknown_dispatch_counts_from_the_settle_time():
    _, monitor, clock = make([])
    close = BarClose(at=M0, interval=Interval.MIN_1, bars=(), sequence=99)
    clock.set(M0 + timedelta(seconds=4))
    assert monitor.record_order(close) == pytest.approx(2.0)


def test_snapshot_carries_driver_stream_and_latency():
    health = StreamHealth(source="replay", state="streaming", bars_written=4, late_ticks=1)
    driver, monitor, _ = make([bar(0), bar(1)], health=health, calendar="24/7")

    def boom(event):
        raise RuntimeError("x")

    driver.register(boom, name="broken")
    driver.run(["A.US"])
    snap = monitor.snapshot()
    assert snap["engine_id"] == "e1"
    assert snap["calendar"] == "24/7"
    assert snap["state"] == "streaming"
    assert snap["driver"]["bar_closes"] == 2
    assert snap["driver"]["handler_errors"] == {"broken": 2}
    assert snap["stream"]["bars_written"] == 4
    assert snap["latency"]["dispatch_lag"]["counts"][0] == 2
    assert snap["last_dispatch_at"] == monitor.last_dispatch_at.isoformat()


def test_snapshot_without_a_stream_says_running():
    _, monitor, _ = make([])
    snap = monitor.snapshot()
    assert snap["state"] == "running"
    assert snap["stream"] is None


def test_monitor_publishes_to_the_status_row(state_path):
    store = EngineStatusStore(state_path)
    ticks = iter([0.0, 1.0, 2.0])
    driver, monitor, _ = make(
        [bar(0), bar(1), bar(2)], store=store, publish_seconds=30, wall=lambda: next(ticks)
    )
    monitor.start()
    driver.run(["A.US"])
    [row] = store.read_all()
    assert row.engine_id == "e1"
    assert row.stopped_at is None
    assert row.last_dispatch_at is not None
    # throttled: only the first close publishes within 30s of wall time
    assert row.snapshot["driver"]["bar_closes"] == 1
    monitor.stop()
    [row] = store.read_all()
    assert row.stopped_at is not None
    assert row.state == "stopped"
    assert row.snapshot["driver"]["bar_closes"] == 3


def test_a_failing_publish_never_stops_the_engine(state_path, tmp_path):
    class Broken(EngineStatusStore):
        def write(self, snapshot, *, now):
            raise OSError("disk full")

    driver, monitor, _ = make([bar(0), bar(1)], store=Broken(state_path))
    driver.run(["A.US"])
    assert driver.stats.bar_closes == 2
    assert driver.stats.handler_errors == {}
