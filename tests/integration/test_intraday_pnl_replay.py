"""Live marks and intraday P&L on a replayed stream (roadmap 21.3.3).

A recording goes through the ``replay`` source and the event driver, the
tracker marks every bar close, books the day's fills when their time comes,
and stores one row per book every five minutes."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from stonks.core.clock import FakeClock
from stonks.core.stream import QuoteTick, TradeTick
from stonks.engine.driver import EventDriver
from stonks.production.intraday_pnl import (
    IntradayPnlSettings,
    IntradayPnlTracker,
    MarkBook,
    latest_intraday_day,
    list_intraday_snapshots,
    load_day_start,
)
from stonks.store.state import SqliteState
from stonks.streaming.recorder import StreamRecorder
from stonks.streaming.sources.replay import ReplaySource

DAY = date(2026, 9, 28)
OPEN = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
ONE = timedelta(minutes=1)
PF = "pf_default"


def _strategy(state: SqliteState, sid: str) -> None:
    state.execute(
        "INSERT INTO strategies (id, class_path, params_json, status, created_at, updated_at)"
        " VALUES (?, 'x:Y', '{}', 'shadow', '2026-01-01', '2026-01-01')",
        [sid],
    )


def _start_of_day(state: SqliteState) -> None:
    """Yesterday's tick left 10 A.US (7 for mom, 3 for rev) and 1000 cash."""
    _strategy(state, "mom")
    _strategy(state, "rev")
    state.execute(
        "INSERT INTO tick_runs (id, started_at, status) VALUES ('t1', '2026-09-25', 'ok')"
    )
    state.execute(
        "INSERT INTO portfolio_snapshots"
        " (tick_id, taken_at, cash, positions_json, total_value, as_of, portfolio_id)"
        " VALUES ('t1', '2026-09-25T21:00:00+00:00', 1000, ?, 2000, '2026-09-25', ?)",
        [json.dumps({"A.US": 10}), PF],
    )
    for sid, share in (("mom", 0.7), ("rev", 0.3)):
        state.execute(
            "INSERT INTO position_attribution (tick_id, portfolio_id, as_of, ticker,"
            " strategy_id, quantity, weight_share, source, created_at)"
            " VALUES ('t1', ?, '2026-09-25', 'A.US', ?, 10, ?, 'carried', '2026-09-25')",
            [PF, sid, share],
        )


def _fill(
    state: SqliteState,
    cid: str,
    ticker: str,
    side: str,
    qty: float,
    price: float,
    at: datetime,
    strategy: str | None,
    fee: float = 0.0,
) -> None:
    state.execute(
        "INSERT INTO orders (client_id, strategy_id, ticker, side, quantity, order_type,"
        " status, created_at, updated_at, portfolio_id)"
        " VALUES (?, ?, ?, ?, ?, 'market', 'filled', ?, ?, ?)",
        [cid, strategy, ticker, side, qty, at.isoformat(), at.isoformat(), PF],
    )
    state.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at,"
        " portfolio_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [cid, ticker, qty, price, fee, at.isoformat(timespec="seconds"), PF],
    )


def _record(tmp_path, minutes: int = 12):
    """A.US trades one dollar higher each minute from 101, B.US quotes."""
    root = tmp_path / "rec"
    with StreamRecorder(root) as rec:
        for m in range(minutes):
            at = OPEN + m * ONE + timedelta(seconds=10)
            rec.write(TradeTick("A.US", at, 101.0 + m, 10, source="eodhd"))
            rec.write(QuoteTick("B.US", at, bid=49.9, ask=50.1, source="eodhd"))
        # a last trade well after, so every minute closes
        rec.write(TradeTick("A.US", OPEN + (minutes + 2) * ONE, 101.0 + minutes, 1))
    return root


def _run(state: SqliteState, root, *, marks: MarkBook | None = None) -> IntradayPnlTracker:
    clock = FakeClock(OPEN)
    source = ReplaySource(root, clock=clock)
    driver = EventDriver(source, clock=clock)
    tracker = IntradayPnlTracker(
        state,
        [PF],
        marks=marks,
        reference_prices=lambda tickers, day: {"A.US": 100.0},
        settings=IntradayPnlSettings(snapshot_minutes=5),
        clock=clock,
    )
    driver.register(tracker)
    driver.run(["A.US", "B.US"])
    tracker.finish()
    return tracker


@pytest.fixture
def seeded(state):
    _start_of_day(state)
    return state


def test_day_start_reads_yesterdays_snapshot_and_sleeves(seeded):
    start = load_day_start(seeded, PF, DAY)
    assert start.as_of == date(2026, 9, 25) and start.cash == 1000.0
    assert start.positions == {"A.US": 10.0}
    assert start.sleeves == {
        "mom": {"A.US": pytest.approx(7.0)},
        "rev": {"A.US": pytest.approx(3.0)},
    }


def test_replay_marks_and_stores_a_row_per_book_every_five_minutes(seeded, tmp_path):
    tracker = _run(seeded, _record(tmp_path))
    assert tracker.marks.price("A.US") == 113.0
    assert tracker.marks.price("B.US") == pytest.approx(50.0)
    assert latest_intraday_day(seeded, PF) == DAY
    rows, total = list_intraday_snapshots(seeded, PF, DAY)
    # bar closes 13:31 .. 13:45: buckets 13:30, 13:35, 13:40, 13:45 then the finish
    assert [r.at.strftime("%H:%M") for r in reversed(rows)] == [
        "13:31",
        "13:35",
        "13:40",
        "13:45",
    ]
    assert total == 4
    last = rows[0]
    # 10 shares from the 100 prior close to the 113 last mark
    assert last.pnl == pytest.approx(130.0) and last.unrealised == pytest.approx(130.0)
    assert last.start_value == pytest.approx(2000.0) and last.value == pytest.approx(2130.0)
    assert last.day_return == pytest.approx(0.065)
    assert last.gross_exposure == pytest.approx(1130.0)
    assert last.high_water_pnl == pytest.approx(130.0) and last.drawdown == 0.0
    sleeves, n = list_intraday_snapshots(seeded, PF, DAY, strategy_id="mom")
    assert n == 4 and sleeves[0].pnl == pytest.approx(91.0)
    everything, n_all = list_intraday_snapshots(seeded, PF, DAY, strategy_id=None)
    assert n_all == 12 and {r.strategy_id for r in everything} == {"", "mom", "rev"}


def test_fills_are_booked_when_their_time_comes(seeded, tmp_path):
    # mom sells 7 at 105 at 13:34:30 with a 1.00 fee, a manual buy of B.US at 13:37
    _fill(
        seeded, "c1", "A.US", "sell", 7, 105.0, OPEN + timedelta(minutes=4, seconds=30), "mom", 1.0
    )
    _fill(seeded, "c2", "B.US", "buy", 4, 50.5, OPEN + 7 * ONE, None)
    tracker = _run(seeded, _record(tmp_path))
    rows, _ = list_intraday_snapshots(seeded, PF, DAY)
    by_time = {r.at.strftime("%H:%M"): r for r in rows}
    assert by_time["13:31"].fills == 0
    assert by_time["13:35"].fills == 1 and by_time["13:35"].realised == pytest.approx(35.0)
    last = by_time["13:45"]
    assert last.fills == 2 and last.fees == pytest.approx(1.0)
    # realised 35, A.US 3 left: 3 * 13 = 39, B.US 4 * (50 - 50.5) = -2, fee 1
    assert last.pnl == pytest.approx(35 + 39 - 2 - 1)
    mom, _ = list_intraday_snapshots(seeded, PF, DAY, strategy_id="mom")
    assert mom[0].realised == pytest.approx(35.0) and mom[0].unrealised == 0.0
    assert mom[0].gross_exposure == 0.0 and mom[0].pnl == pytest.approx(34.0)
    assert tracker.snapshots_written == 12


def test_the_high_water_mark_and_drawdown_follow_the_marks(seeded, tmp_path):
    root = tmp_path / "rec"
    prices = [110, 120, 115, 105, 104, 103, 102, 101]
    with StreamRecorder(root) as rec:
        for m, p in enumerate(prices):
            rec.write(TradeTick("A.US", OPEN + m * ONE + timedelta(seconds=5), float(p), 1))
        rec.write(TradeTick("A.US", OPEN + 20 * ONE, 101.0, 1))
    _run(seeded, root)
    rows, _ = list_intraday_snapshots(seeded, PF, DAY)
    last = rows[0]
    assert last.high_water_pnl == pytest.approx(200.0)  # 10 shares at 120
    assert last.pnl == pytest.approx(10.0)
    assert last.drawdown == pytest.approx((10 - 200) / 2200)


def test_a_restart_keeps_the_days_high(seeded, tmp_path):
    _run(seeded, _record(tmp_path, minutes=12))
    # later in the day the price is back at 105: pnl 50, high still 130
    root = tmp_path / "later"
    with StreamRecorder(root) as rec:
        at = OPEN + 60 * ONE
        rec.write(TradeTick("A.US", at, 105.0, 1))
        rec.write(TradeTick("A.US", at + 3 * ONE, 105.0, 1))
    _run(seeded, root)
    rows, _ = list_intraday_snapshots(seeded, PF, DAY)
    assert rows[0].pnl == pytest.approx(50.0)
    assert rows[0].high_water_pnl == pytest.approx(130.0) and rows[0].drawdown < 0


def test_stale_and_missing_marks_are_counted(seeded, tmp_path):
    root = tmp_path / "rec"
    with StreamRecorder(root) as rec:
        rec.write(TradeTick("A.US", OPEN + timedelta(seconds=5), 101.0, 1))
        # only B.US trades afterwards: A.US's mark ages
        for m in range(1, 8):
            rec.write(TradeTick("B.US", OPEN + m * ONE + timedelta(seconds=5), 50.0, 1))
        rec.write(TradeTick("B.US", OPEN + 10 * ONE, 50.0, 1))
    _fill(seeded, "c3", "C.US", "buy", 1, 10.0, OPEN + ONE, None)
    tracker = IntradayPnlTracker(
        seeded,
        [PF],
        reference_prices=lambda tickers, day: {"A.US": 100.0},
        settings=IntradayPnlSettings(snapshot_minutes=5, stale_mark_seconds=120),
    )
    clock = FakeClock(OPEN)
    driver = EventDriver(ReplaySource(root, clock=clock), clock=clock)
    driver.register(tracker)
    driver.run(["A.US", "B.US"])
    tracker.finish()
    rows, _ = list_intraday_snapshots(seeded, PF, DAY)
    last = rows[0]
    assert last.unmarked == 1  # C.US never traded on the stream
    assert last.stale_marks == 1 and last.max_mark_age_seconds == pytest.approx(600.0)


def test_a_runner_subscriber_mark_book_sees_ticks_between_bars(seeded, tmp_path):
    marks = MarkBook()
    marks(TradeTick("A.US", OPEN + 30 * ONE, 150.0, 1))
    tracker = IntradayPnlTracker(seeded, [PF], marks=marks, reference_prices=lambda t, d: {})
    [book, *_] = tracker.books(OPEN + 31 * ONE)
    # no prior close: priced at the first mark, so no P&L yet
    assert book.pnl == 0.0 and book.start_value == pytest.approx(2500.0)
    marks(TradeTick("A.US", OPEN + 32 * ONE, 151.0, 1))
    [book, *_] = tracker.books(OPEN + 33 * ONE)
    assert book.pnl == pytest.approx(10.0)
