"""The stream runner end to end on loopback (roadmap 21.1): a fake EODHD
server that drops the first connection, reconnect and backfill, bars in the
lake, and a recording replayed into the same bars."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from stonks.core.clock import FakeClock
from stonks.core.interval import Interval
from stonks.core.stream import TradeTick
from stonks.streaming.recorder import StreamRecorder, read_recording
from stonks.streaming.runner import StreamRunner
from stonks.streaming.settings import StreamingSettings
from stonks.streaming.sources.eodhd_ws import EodhdStreamSource
from stonks.streaming.sources.replay import ReplaySource
from tests.fakes.eodhd_ws import FakeEodhdServer, load_messages

TRADES = load_messages("eodhd_us_trades")
LO, HI = datetime(2026, 1, 1), datetime(2027, 1, 1)
M0 = datetime(2026, 9, 28, 13, 30)


class Backfill:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], datetime, datetime]] = []

    def backfill(self, tickers, start, end):
        self.calls.append((list(tickers), start, end))
        return len(tickers)


def cfg(**kw) -> StreamingSettings:
    base = {
        "tickers": ["AAPL.US", "MSFT.US"],
        "min_gap_seconds": 0,
        "backfill_delay_seconds": 0,
        "backoff": {"initial_seconds": 0.05, "max_seconds": 0.2, "jitter": 0},
    }
    base.update(kw)
    return StreamingSettings.model_validate(base)


def stop_after_trades(runner_box: list[StreamRunner], n: int):
    seen = {"n": 0}

    def on_event(ev):
        if isinstance(ev, TradeTick):
            seen["n"] += 1
            if seen["n"] >= n:
                runner_box[0].stop()

    return on_event


def run_in_thread(runner: StreamRunner) -> threading.Thread:
    t = threading.Thread(target=runner.run, daemon=True)
    t.start()
    return t


def test_reconnects_after_a_drop_backfills_and_writes_bars(lake):
    with FakeEodhdServer({"us": TRADES}, drop_first_after=5) as server:
        src = EodhdStreamSource(server.api_key, url=server.url, heartbeat_seconds=0.05)
        box: list[StreamRunner] = []
        backfill = Backfill()
        runner = StreamRunner(
            src,
            cfg(),
            store=lake.bar_store,
            backfiller=backfill,
            subscribers=[stop_after_trades(box, len(TRADES))],
            # before the recorded session (2026-09-28 13:30 UTC): only the
            # trades close bars, whatever the wall clock says
            clock=FakeClock(datetime(2026, 9, 28, tzinfo=UTC)),
        )
        box.append(runner)
        t = run_in_thread(runner)
        t.join(20)
        assert not t.is_alive()
    h = runner.health
    assert h.connects == 2 and h.disconnects == 1 and h.state == "stopped"
    assert server.connects == ["us", "us"]
    assert len(backfill.calls) == 1 and backfill.calls[0][0] == ["AAPL.US", "MSFT.US"]
    assert h.gaps[0].reason == "disconnect" and h.gaps[0].backfilled is True
    aapl = lake.get_bars("AAPL.US", Interval.MIN_1, LO, HI)
    # minute 0 and 1 closed by later trades, minute 2 still open at stop
    assert list(aapl["timestamp"].dt.minute) == [30, 31]
    first = aapl.iloc[0]
    assert (first["open"], first["high"], first["low"], first["close"], first["volume"]) == (
        227.31,
        227.45,
        227.12,
        227.20,
        500,
    )
    assert len(lake.get_bars("MSFT.US", Interval.MIN_1, LO, HI)) == 2


def test_a_recording_replays_into_the_same_bars(lake, tmp_path):
    # record a live session from the fake server
    with FakeEodhdServer({"us": TRADES}) as server:
        src = EodhdStreamSource(server.api_key, url=server.url, heartbeat_seconds=0.05)
        box: list[StreamRunner] = []
        live = StreamRunner(
            src,
            cfg(backfill=False),
            recorder=StreamRecorder(tmp_path),
            subscribers=[stop_after_trades(box, len(TRADES))],
        )
        box.append(live)
        t = run_in_thread(live)
        t.join(20)
        assert not t.is_alive()
    recorded = [e for e in read_recording(tmp_path) if isinstance(e, TradeTick)]
    assert [e.price for e in recorded] == [m["p"] for m in TRADES]

    # replay it on event time into the lake
    clock = FakeClock(datetime(2026, 9, 28, tzinfo=UTC))
    replay = StreamRunner(
        ReplaySource(tmp_path, clock=clock), cfg(backfill=False), store=lake.bar_store, clock=clock
    )
    health = replay.run()
    assert health.state == "stopped" and health.connects == 1
    aapl = lake.get_bars("AAPL.US", Interval.MIN_1, LO, HI)
    msft = lake.get_bars("MSFT.US", Interval.MIN_1, LO, HI)
    assert list(aapl["timestamp"].dt.minute) == [30, 31, 32]
    assert list(msft["timestamp"].dt.minute) == [30, 31, 32]
    assert list(aapl["volume"]) == [500, 100, 15]
    assert aapl["close"].iloc[1] == pytest.approx(227.55)

    # a second replay changes nothing (idempotent upserts)
    StreamRunner(
        ReplaySource(tmp_path, clock=clock), cfg(backfill=False), store=lake.bar_store, clock=clock
    ).run()
    assert len(lake.get_bars("AAPL.US", Interval.MIN_1, LO, HI)) == 3
    assert aapl["timestamp"].iloc[0] == M0
