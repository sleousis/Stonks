"""EarningsDrift: post-earnings announcement drift, read point in time (P12)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.core.types import Portfolio
from stonks.lab.catalog import strategy_catalog
from stonks.store.lake import DuckDBLake
from stonks.store.pit import PitSession
from stonks.strategies.examples.earnings_drift import EarningsDrift

BASE = date(2025, 1, 6)
N_PEERS = 25


def _weekday(day: date) -> date:
    return pd.Timestamp(np.busday_offset(np.datetime64(day), 0, roll="forward")).date()


def _sessions_after(day: date, n: int) -> date:
    return pd.Timestamp(np.busday_offset(np.datetime64(day), n, roll="forward")).date()


def _row(ticker: str, q: int, report: date, actual: float | None, timing: str | None) -> dict:
    return {
        "ticker": ticker,
        "period_end": BASE - timedelta(days=30) + timedelta(days=91 * q),
        "report_date": report,
        "before_after_market": timing,
        "currency": "USD",
        "eps_estimate": 1.0,
        "eps_actual": actual,
        "eps_difference": None if actual is None else actual - 1.0,
        "surprise_percent": None,
        "source": "t",
        "updated_at": datetime(2026, 9, 1),
    }


def _history(ticker: str, offset: int, last: float | None, timing: str | None) -> list[dict]:
    """Five ordinary quarters, then a sixth with surprise ``last``."""
    rows = []
    for q in range(6):
        report = _weekday(BASE + timedelta(days=91 * q + offset))
        actual = 1.0 + 0.01 * ((offset + q) % 5 - 2) if q < 5 else last
        rows.append(_row(ticker, q, report, None if actual is None else actual, timing))
    return rows


WIN_DAY = _weekday(BASE + timedelta(days=455 + 30))


def _rows(future: bool) -> list[dict]:
    rows = []
    for i in range(N_PEERS):
        rows += _history(f"P{i:02d}.US", i, 1.0 + 0.01 * (i % 5 - 2), "before")
    rows += _history("WIN.US", 30, 1.5, "after")  # a huge beat after the close
    rows += _history("MEH.US", 30, 1.0, "after")  # met the estimate
    rows += _history("NOW.US", 30, 1.5, "during")  # a beat in the session
    # an upcoming report: an estimate, no actual yet
    rows.append(_row("WIN.US", 6, _weekday(WIN_DAY + timedelta(days=91)), None, "before"))
    if future:
        # reports after the decision day that would flip every answer
        rows.append(_row("MEH.US", 7, _sessions_after(WIN_DAY, 5), 9.0, "before"))
        rows.append(_row("WIN.US", 7, _sessions_after(WIN_DAY, 6), -9.0, "before"))
        rows.append(_row("P00.US", 7, _sessions_after(WIN_DAY, 5), 9.0, None))
    return rows


def _lake(future: bool) -> DuckDBLake:
    db = DuckDBLake(Path(":memory:"))
    db.migrate()
    frame = pd.DataFrame(_rows(future))
    db.con.register("_e", frame)
    db.con.execute("INSERT INTO earnings_calendar SELECT * FROM _e")
    db.con.unregister("_e")
    return db


@pytest.fixture(scope="module")
def lakes():
    past, planted = _lake(False), _lake(True)
    yield past, planted
    past.close()
    planted.close()


def _at(lake, day: date, ticker: str, params: dict | None = None, interval=Interval.DAY_1):
    stamp = datetime.combine(day, datetime.min.time())
    view = PitSession(lake).at(stamp, decision_interval=interval)
    return EarningsDrift(params or {}).estimate_return(ticker, stamp, view)


def test_catalogued_with_a_plain_card():
    cls = strategy_catalog()["earnings_drift"]
    assert cls is EarningsDrift
    assert cls.title and cls.summary
    assert cls.applicable_asset_classes == ("equity",)
    spec = {s.name: s for s in cls.parameter_spec()}
    assert spec["hold_days"].default == 40


def test_a_top_bucket_beat_is_held_for_hold_days_from_the_first_session_after_it(lakes):
    past, _ = lakes
    assert _at(past, WIN_DAY - timedelta(days=1), "WIN.US") is None  # not out yet
    score = _at(past, WIN_DAY, "WIN.US")  # out after the close, bought at the next open
    assert score is not None and score > 0
    first = _sessions_after(WIN_DAY, 1)  # the first session that can react
    assert _at(past, _sessions_after(first, 39), "WIN.US") == pytest.approx(score)
    assert _at(past, _sessions_after(first, 40), "WIN.US") is None
    assert _at(past, WIN_DAY, "WIN.US", {"hold_days": 5}) is not None
    assert _at(past, _sessions_after(first, 5), "WIN.US", {"hold_days": 5}) is None


def test_a_report_in_line_with_the_estimate_is_no_pick(lakes):
    past, _ = lakes
    assert _at(past, WIN_DAY, "MEH.US") is None


def test_a_report_out_in_the_session_counts_only_after_that_session_closes(lakes):
    past, _ = lakes
    assert _at(past, WIN_DAY, "NOW.US") is not None  # daily: decided at the close
    # mid-session the report is not out yet
    noon = datetime.combine(WIN_DAY, datetime.min.time()) + timedelta(hours=15)
    view = PitSession(past).at(noon, decision_interval=Interval.HOUR_1)
    assert EarningsDrift({}).estimate_return("NOW.US", noon, view) is None


def test_too_few_peers_or_too_little_history_is_no_pick(lakes):
    past, _ = lakes
    assert _at(past, WIN_DAY, "WIN.US", {"min_peers": N_PEERS + 10}) is None
    assert _at(past, WIN_DAY, "NONE.US") is None


def test_no_look_ahead_reports_after_the_decision_change_nothing(lakes):
    past, planted = lakes
    for day in (WIN_DAY, _sessions_after(WIN_DAY, 1), _sessions_after(WIN_DAY, 2)):
        for ticker in ("WIN.US", "MEH.US", "NOW.US", "P00.US"):
            assert _at(past, day, ticker) == _at(planted, day, ticker), (day, ticker)


def test_no_look_ahead_on_a_raw_lake_too(lakes):
    """Handed the lake itself, the strategy still reads only reports out by
    the end of the decision day."""
    past, planted = lakes
    s = EarningsDrift({})
    for day in (WIN_DAY, _sessions_after(WIN_DAY, 1), _sessions_after(WIN_DAY, 2)):
        for ticker in ("WIN.US", "MEH.US", "P00.US"):
            assert s.estimate_return(ticker, day, planted) == s.estimate_return(ticker, day, past)
            assert s.estimate_return(ticker, day, past) == _at(past, day, ticker)


def test_a_lake_without_calendars_gives_no_pick():
    assert EarningsDrift({}).estimate_return("WIN.US", WIN_DAY, object()) is None


def test_decide_buys_new_picks_equally_and_sells_the_rest():
    s = EarningsDrift({})
    book = Portfolio(cash=1000.0, positions={"OLD.US": 5.0})
    orders = s.decide(
        [(3.0, "A.US"), (2.0, "B.US")], book, {"A.US": 10.0, "B.US": 20.0}, date(2026, 5, 7)
    )
    by = {(o.side, o.ticker): o.quantity for o in orders}
    assert by == {("sell", "OLD.US"): 5.0, ("buy", "A.US"): 50.0, ("buy", "B.US"): 25.0}


def test_a_universe_sets_the_peers_and_the_names(lakes):
    past, _ = lakes
    peers = ",".join(f"P{i:02d}.US" for i in range(N_PEERS))
    assert _at(past, WIN_DAY, "WIN.US", {"universe": f"{peers},WIN.US"}) is not None
    assert _at(past, WIN_DAY, "WIN.US", {"universe": peers}) is None  # not in it
    few = ",".join(f"P{i:02d}.US" for i in range(5))
    assert _at(past, WIN_DAY, "WIN.US", {"universe": f"{few},WIN.US"}) is None  # too few peers
