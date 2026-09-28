"""The supervised stream runner (roadmap 21.1)."""

from __future__ import annotations

import random
import threading
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta

import pytest

from stonks.config import Settings
from stonks.core.clock import FakeClock
from stonks.core.interval import Interval
from stonks.core.stream import Heartbeat, StreamEvent, TradeTick
from stonks.scheduling.calendar import Session
from stonks.scheduling.metrics import render_prometheus
from stonks.streaming.base import (
    StreamAuthError,
    StreamContext,
    StreamDisconnectedError,
    StreamError,
    StreamingSource,
)
from stonks.streaming.health import stream_metric_families
from stonks.streaming.recorder import StreamRecorder, read_recording
from stonks.streaming.runner import Backoff, PipelineBackfiller, StreamRunner
from stonks.streaming.settings import StreamBackoffSettings, StreamingSettings

OPEN = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
LO, HI = datetime(2026, 1, 1), datetime(2027, 1, 1)


def at(seconds: float) -> datetime:
    return OPEN + timedelta(seconds=seconds)


def trade(seconds: float, price: float = 10.0, ticker: str = "A.US") -> TradeTick:
    return TradeTick(ticker, at(seconds), price, 1, "fake")


class FakeSource(StreamingSource):
    """Plays scripted sessions. Each session is a list of events, ended by
    an exception to raise (a disconnect) or by nothing (the feed ended).
    Sets the fake clock to each event's time, like a replay."""

    source_id = "fake"

    def __init__(self, sessions, clock: FakeClock, *, finite: bool = False):
        self.sessions = list(sessions)
        self.clock = clock
        self.opened = 0
        self._finite = finite
        self.runner: StreamRunner | None = None
        self.closed = False

    @classmethod
    def from_settings(cls, ctx: StreamContext):  # pragma: no cover - not built from settings
        raise NotImplementedError

    @property
    def finite(self):  # type: ignore[override]
        return self._finite

    def stream(self, tickers: Sequence[str]) -> Iterator[StreamEvent]:
        self.opened += 1
        if not self.sessions:
            assert self.runner is not None
            self.runner.stop()
            return
        session = self.sessions.pop(0)
        if isinstance(session, BaseException):
            raise session
        for item in session:
            if isinstance(item, BaseException):
                raise item
            self.clock.set(item.timestamp)
            yield item
        if not self.sessions and self.runner is not None:
            self.runner.stop()  # the script is over

    def close(self) -> None:
        self.closed = True


class FakeHours:
    def __init__(self, open_: datetime = OPEN, close: datetime = OPEN + timedelta(hours=6.5)):
        self.open, self.close = open_, close

    def is_open_at(self, when: datetime) -> bool:
        return self.open <= when < self.close

    def session(self, day: date) -> Session | None:
        return Session(day, self.open, self.close) if day == self.open.date() else None


class FakeBackfiller:
    def __init__(self, fail: bool = False):
        self.calls: list[tuple[list[str], datetime, datetime]] = []
        self.fail = fail

    def backfill(self, tickers, start, end):
        self.calls.append((list(tickers), start, end))
        if self.fail:
            raise RuntimeError("vendor down")
        return len(tickers)


def settings(**kw) -> StreamingSettings:
    base = {
        "tickers": ["A.US"],
        "bar_grace_seconds": 0,
        "flush_seconds": 1,
        "stale_after_seconds": 60,
        "min_gap_seconds": 60,
        "backfill_delay_seconds": 0,
        "backoff": {"initial_seconds": 1, "max_seconds": 8, "jitter": 0},
    }
    base.update(kw)
    return StreamingSettings.model_validate(base)


def runner(sessions, *, lake=None, finite=False, clock=None, cfg=None, **kw):
    clock = clock or FakeClock(at(-1))
    src = FakeSource(sessions, clock, finite=finite)
    sleeps: list[float] = []
    r = StreamRunner(
        src,
        cfg or settings(),
        store=lake.bar_store if lake is not None else None,
        clock=clock,
        sleep=sleeps.append,
        **kw,
    )
    src.runner = r
    return r, src, sleeps


def stored(lake, ticker="A.US"):
    return lake.get_bars(ticker, Interval.MIN_1, LO, HI)


def test_a_finite_stream_writes_every_bar_and_stops(lake):
    r, src, sleeps = runner(
        [[trade(1, 10.0), trade(30, 11.0), trade(61, 12.0), trade(90, 13.0)]],
        lake=lake,
        finite=True,
    )
    health = r.run()
    assert health.state == "stopped" and src.opened == 1 and sleeps == []
    got = stored(lake)
    assert list(got["close"]) == [11.0, 13.0]
    assert health.bars_written == 2 and health.events["trade"] == 4
    assert health.connects == 1


def test_reconnects_with_backoff_and_backfills_the_gap(lake):
    backfill = FakeBackfiller()
    r, src, sleeps = runner(
        [
            [trade(1), trade(61), StreamDisconnectedError("dropped")],
            StreamDisconnectedError("still down"),
            [trade(400), trade(500)],
        ],
        lake=lake,
        backfiller=backfill,
    )
    health = r.run()
    assert sleeps == [1, 2]  # doubling backoff, no jitter
    assert health.disconnects == 2 and health.connects == 2
    ((tickers, start, end),) = backfill.calls
    assert tickers == ["A.US"] and start == at(61) and end == at(400)
    assert health.backfills_ok == 1
    assert [g.reason for g in health.gaps] == ["disconnect"]
    assert health.gaps[0].backfilled is True
    assert "dropped" in (health.last_error or "") or "still down" in (health.last_error or "")


def test_backoff_resets_after_a_good_connection(lake):
    r, src, sleeps = runner(
        [
            [trade(1), StreamDisconnectedError("a")],
            [trade(100), StreamDisconnectedError("b")],
            [trade(200)],
        ],
        lake=lake,
    )
    r.run()
    assert sleeps == [1, 1]


def test_backfill_waits_for_the_delay(lake):
    backfill = FakeBackfiller()
    r, _, _ = runner(
        [[trade(1), StreamDisconnectedError("x")], [trade(300), trade(350), trade(430)]],
        lake=lake,
        backfiller=backfill,
        cfg=settings(backfill_delay_seconds=120),
    )
    r.run()
    ((_, start, end),) = backfill.calls
    assert start == at(1) and end == at(300)


def test_short_gaps_are_not_backfilled(lake):
    backfill = FakeBackfiller()
    r, _, _ = runner(
        [[trade(1), StreamDisconnectedError("x")], [trade(20)]],
        lake=lake,
        backfiller=backfill,
    )
    health = r.run()
    assert backfill.calls == []
    assert health.gaps[0].backfilled is None


def test_a_failed_backfill_is_counted_and_the_stream_goes_on(lake):
    r, _, _ = runner(
        [[trade(1), StreamDisconnectedError("x")], [trade(300), trade(400)]],
        lake=lake,
        backfiller=FakeBackfiller(fail=True),
    )
    health = r.run()
    assert health.backfills_failed == 1 and health.gaps[0].backfilled is False
    assert list(stored(lake)["timestamp"]) == [
        at(0).replace(tzinfo=None),
        at(300).replace(tzinfo=None),
    ]


def test_an_auth_error_is_never_retried(lake):
    r, src, sleeps = runner([StreamAuthError("bad key")], lake=lake)
    with pytest.raises(StreamAuthError):
        r.run()
    assert r.health.state == "failed" and sleeps == [] and src.opened == 1


def test_gives_up_after_max_reconnects(lake):
    r, _, sleeps = runner(
        [StreamDisconnectedError("a"), StreamDisconnectedError("b"), StreamDisconnectedError("c")],
        lake=lake,
        max_reconnects=2,
    )
    with pytest.raises(StreamError, match="2 reconnects"):
        r.run()
    assert r.health.state == "failed" and len(sleeps) == 2


def test_a_silent_stream_in_market_hours_is_stale(lake):
    beats = [Heartbeat(at(s)) for s in (5, 30, 55, 70)]
    r, src, sleeps = runner(
        [[trade(1), *beats], [trade(100)]], lake=lake, hours=FakeHours(), cfg=settings()
    )
    health = r.run()
    assert src.opened == 2 and sleeps == [1]
    assert [g.reason for g in health.gaps] == ["stale"]


def test_silence_outside_market_hours_is_fine(lake):
    shut = FakeHours(OPEN + timedelta(hours=1), OPEN + timedelta(hours=2))
    beats = [Heartbeat(at(s)) for s in (5, 60, 200, 400)]
    r, src, sleeps = runner([[trade(1), *beats]], lake=lake, hours=shut)
    health = r.run()
    assert sleeps == [] and src.opened == 1
    assert "stale" not in [g.reason for g in health.gaps]


def test_startup_inside_the_session_backfills_from_the_open(lake):
    backfill = FakeBackfiller()
    clock = FakeClock(at(900))
    r, _, _ = runner(
        [[trade(905), trade(1000)]],
        lake=lake,
        finite=True,
        clock=clock,
        hours=FakeHours(),
        backfiller=backfill,
    )
    health = r.run()
    ((_, start, end),) = backfill.calls
    assert start == OPEN and end == at(905)
    assert health.gaps[0].reason == "startup"


def test_no_startup_backfill_before_the_open(lake):
    backfill = FakeBackfiller()
    clock = FakeClock(at(-600))
    r, _, _ = runner(
        [[trade(1)]], lake=lake, finite=True, clock=clock, hours=FakeHours(), backfiller=backfill
    )
    r.run()
    assert backfill.calls == []


def test_backfill_off_in_settings(lake):
    backfill = FakeBackfiller()
    r, _, _ = runner(
        [[trade(1), StreamDisconnectedError("x")], [trade(300)]],
        lake=lake,
        backfiller=backfill,
        cfg=settings(backfill=False),
    )
    r.run()
    assert backfill.calls == []


class FlakyStore:
    def __init__(self, inner):
        self.inner = inner
        self.fail_next = 1

    def upsert(self, frame):
        if self.fail_next:
            self.fail_next -= 1
            raise OSError("busy")
        return self.inner.upsert(frame)


def test_a_write_error_keeps_the_bars_for_later(lake):
    clock = FakeClock(at(-1))
    src = FakeSource([[trade(1), trade(61), trade(122), trade(183)]], clock, finite=True)
    r = StreamRunner(
        src,
        settings(flush_seconds=0.001),
        store=FlakyStore(lake.bar_store),  # type: ignore[arg-type]
        clock=clock,
        sleep=lambda s: None,
    )
    health = r.run()
    assert health.write_errors == 1
    assert len(stored(lake)) == 4


def test_subscribers_see_events_and_bars_and_their_errors_are_counted(lake):
    events, bars = [], []

    def broken(ev):
        raise ValueError("oops")

    r, _, _ = runner(
        [[trade(1), trade(61)]],
        lake=lake,
        finite=True,
        subscribers=[events.append, broken],
        bar_subscribers=[bars.append],
    )
    health = r.run()
    assert len(events) == 2 and [b.timestamp for b in bars] == [OPEN, at(60)]
    assert health.subscriber_errors == 2


def test_stop_flushes_finished_bars_only(lake):
    clock = FakeClock(at(-1))
    gate = threading.Event()

    class Live(FakeSource):
        def stream(self, tickers):
            self.opened += 1
            for ev in (trade(1), trade(61)):
                self.clock.set(ev.timestamp)
                yield ev
            gate.set()
            while not self.closed:
                yield Heartbeat(self.clock.now())
                threading.Event().wait(0.01)

    src = Live([], clock)
    r = StreamRunner(src, settings(flush_seconds=3600), store=lake.bar_store, clock=clock)
    t = threading.Thread(target=r.run)
    t.start()
    assert gate.wait(5)
    r.stop()
    t.join(5)
    assert not t.is_alive()
    assert r.health.state == "stopped" and src.closed
    assert list(stored(lake)["timestamp"]) == [OPEN.replace(tzinfo=None)]


def test_tees_events_into_a_recorder(lake, tmp_path):
    rec = StreamRecorder(tmp_path)
    events = [trade(1), Heartbeat(at(20)), trade(61)]
    r, _, _ = runner([events], lake=lake, finite=True, recorder=rec)
    r.run()
    assert list(read_recording(tmp_path)) == events


def test_runs_without_a_store():
    r, _, _ = runner([[trade(1), trade(61)]], finite=True)
    assert r.run().bars_written == 0


def test_metrics_render():
    r, _, _ = runner([[trade(1)]], finite=True)
    r.run()
    text = render_prometheus(stream_metric_families(r.health))
    assert 'stonks_stream_events_total{source="fake",kind="trade"} 1' in text or (
        'stonks_stream_events_total{kind="trade",source="fake"} 1' in text
    )
    assert "stonks_stream_up" in text and "stonks_stream_backfills_total" in text
    snap = r.health.snapshot()
    assert snap["state"] == "stopped" and snap["events"]["trade"] == 1


def test_health_round_trips_through_its_snapshot():
    from stonks.streaming.health import Gap, StreamHealth

    h = StreamHealth(source="eodhd", state="streaming", connects=2, bars_written=5)
    h.last_event_at = at(3)
    h.backfills_ok = 1
    h.add_gap(Gap(at(0), at(2), "disconnect", backfilled=True))
    back = StreamHealth.from_snapshot(h.snapshot())
    assert back.snapshot() == h.snapshot()
    assert back.gaps_total["disconnect"] == 1


def test_health_from_a_bad_snapshot_is_idle():
    from stonks.streaming.health import StreamHealth

    back = StreamHealth.from_snapshot({"state": "weird", "gaps": [{"start": None}]})
    assert back.state == "idle"
    assert back.source == "unknown"
    assert not back.gaps


def test_backoff_grows_caps_and_jitters():
    b = Backoff(StreamBackoffSettings(initial_seconds=1, max_seconds=5, jitter=0))
    assert [b.next_delay() for _ in range(5)] == [1, 2, 4, 5, 5]
    b.reset()
    assert b.next_delay() == 1
    j = Backoff(StreamBackoffSettings(initial_seconds=10, jitter=0.5), rng=random.Random(7))
    delays = [j.next_delay() for _ in range(1)]
    assert 5 <= delays[0] <= 15


def test_pipeline_backfiller_asks_for_minute_bars_by_day():
    calls = []

    class Pipe:
        def run_intraday_bars(self, tickers, interval, since=None, until=None):
            calls.append((list(tickers), interval, since, until))

            class R:
                status, tickers_ok, tickers_failed = "ok", 1, 0

            return R()

    bf = PipelineBackfiller(Pipe())  # type: ignore[arg-type]
    assert bf.backfill(["A.US"], at(0), at(86_400)) == 1
    assert calls == [(["A.US"], Interval.MIN_1, date(2026, 9, 28), date(2026, 9, 29))]


def test_pipeline_backfiller_raises_when_every_ticker_failed():
    class Pipe:
        def run_intraday_bars(self, tickers, interval, since=None, until=None):
            class R:
                status, tickers_ok, tickers_failed = "error", 0, 1

            return R()

    with pytest.raises(StreamError, match="backfill"):
        PipelineBackfiller(Pipe()).backfill(["A.US"], at(0), at(60))  # type: ignore[arg-type]


def test_build_from_settings_uses_the_registry(lake, tmp_path):
    from stonks.streaming.runner import build_runner

    s = Settings()
    s.streaming.source = "replay"
    s.streaming.replay.path = str(tmp_path)
    s.streaming.tickers = ["A.US"]
    s.streaming.backfill = False
    r = build_runner(s, lake=lake, clock=FakeClock(OPEN))
    assert r.source.source_id == "replay" and r.tickers == ["A.US"]
