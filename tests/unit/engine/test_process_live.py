"""The engine in live mode behind the StreamRunner (roadmap 21.2.5). The
live stream is a recording played through the runner, so the test stays
hermetic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stonks.accounts.models import DEFAULT_PORTFOLIO_ID
from stonks.core.clock import FakeClock
from stonks.engine.process import EngineProcess
from stonks.store.lake import DuckDBLake
from stonks.streaming.runner import StreamRunner
from stonks.streaming.settings import StreamingSettings
from stonks.streaming.sources.replay import ReplaySource
from tests.unit.engine.engine_fixtures import book, day_frame, record
from tests.unit.engine.minute_lake import DAYS, TICKERS, MinuteMomentum, session_minutes

PF = DEFAULT_PORTFOLIO_ID
DAY = DAYS[0]


@pytest.fixture(scope="module")
def recording(tmp_path_factory) -> Path:
    return record(day_frame(DAY), tmp_path_factory.mktemp("rec"))


@pytest.fixture
def empty_lake():
    lake = DuckDBLake(Path(":memory:"))
    lake.migrate()
    for ticker in TICKERS:
        lake.con.execute("INSERT INTO instruments (id, asset_class) VALUES (?, 'equity')", [ticker])
    yield lake
    lake.close()


def live_process(lake, state, recording, *, stop_at=None, bar_store=None) -> EngineProcess:
    clock = FakeClock(datetime(2026, 9, 24, 13, 0, tzinfo=UTC))
    runner = StreamRunner(
        ReplaySource(recording),
        StreamingSettings(backfill=False),
        tickers=list(TICKERS),
        clock=clock,
    )
    return EngineProcess(
        strategies={"0": MinuteMomentum({"lookback": 10})},
        books=[book(PF)],
        lake=lake,
        state=state,
        universe=TICKERS,
        session=DAY,
        runner=runner,
        clock=clock,
        stop_at=stop_at,
        bar_store=bar_store,
    )


def test_live_mode_writes_the_decision_bars_before_it_decides(empty_lake, state, recording) -> None:
    process = live_process(empty_lake, state, recording, bar_store=empty_lake.bar_store)
    stats = process.run()
    assert process.mode == "live"
    assert stats.bar_closes >= len(session_minutes(DAY)) - 1
    assert stats.orders_sent > 20, "the step saw the bars the engine wrote"
    stored = empty_lake.con.execute("SELECT count(*) FROM bars WHERE interval = '1m'").fetchone()
    assert stored is not None and stored[0] >= 2 * (len(session_minutes(DAY)) - 1)
    row = state.sql("SELECT mode, status FROM engine_runs")[0]
    assert (row["mode"], row["status"]) == ("live", "stopped")


def test_without_the_bars_the_step_has_nothing_to_decide_on(empty_lake, state, recording) -> None:
    stats = live_process(empty_lake, state, recording).run()
    assert stats.orders_sent == 0


def test_live_mode_stops_itself_after_the_session(empty_lake, state, recording) -> None:
    open_ = session_minutes(DAY)[0].replace(tzinfo=UTC)
    stop_at = open_ + timedelta(minutes=60)
    process = live_process(
        empty_lake, state, recording, stop_at=stop_at, bar_store=empty_lake.bar_store
    )
    stats = process.run()
    assert process.stop_reason == "session over"
    assert stats.last_close_at is not None and stats.last_close_at <= stop_at + timedelta(minutes=1)
    assert stats.bar_closes < 70
