"""Earnings reports read point in time (P12): a report counts from the
latest moment its timing allows, through the lake and the point-in-time
view."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from stonks.calendars.known import report_known_at
from stonks.core.interval import Interval
from stonks.store.lake import DuckDBLake
from stonks.store.pit import PointInTimeLake

D = date(2026, 7, 29)


def test_report_known_at_is_the_latest_moment_its_timing_allows():
    assert report_known_at(D, "before") == datetime(2026, 7, 29, 14, 30)
    assert report_known_at(D, "during") == datetime(2026, 7, 29, 21, 0)
    assert report_known_at(D, "after") == datetime(2026, 7, 30, 0, 0)
    assert report_known_at(D, None) == datetime(2026, 7, 30, 0, 0)
    # a timestamp works too
    assert report_known_at(pd.Timestamp(D), "before") == datetime(2026, 7, 29, 14, 30)


def _row(ticker: str, period_end: date, report: date, timing: str | None) -> dict:
    return {
        "ticker": ticker,
        "period_end": period_end,
        "report_date": report,
        "before_after_market": timing,
        "currency": "USD",
        "eps_estimate": 1.0,
        "eps_actual": 1.2,
        "eps_difference": 0.2,
        "surprise_percent": 20.0,
        "source": "t",
        "updated_at": datetime(2026, 8, 1),
    }


@pytest.fixture
def lake():
    db = DuckDBLake(Path(":memory:"))
    db.migrate()
    frame = pd.DataFrame(
        [
            _row("A.US", date(2026, 6, 30), D, "before"),
            _row("B.US", date(2026, 6, 30), D, "during"),
            _row("C.US", date(2026, 6, 30), D, "after"),
            _row("D.US", date(2026, 6, 30), D, None),
            _row("A.US", date(2026, 3, 31), date(2026, 4, 28), "after"),
        ]
    )
    db.con.register("_e", frame)
    db.con.execute("INSERT INTO earnings_calendar SELECT * FROM _e")
    db.con.unregister("_e")
    yield db
    db.close()


def test_the_lake_reads_reports_oldest_first(lake):
    all_rows = lake.get_earnings_calendar()
    assert len(all_rows) == 5
    assert all_rows["report_date"].iloc[0] == date(2026, 4, 28)
    a = lake.get_earnings_calendar(["A.US"])
    assert list(a["period_end"]) == [date(2026, 3, 31), date(2026, 6, 30)]
    assert lake.get_earnings_calendar([]).empty


def _seen(lake, as_of: datetime, interval: Interval | None) -> set[str]:
    view = PointInTimeLake(lake, as_of, decision_interval=interval)
    rows = view.get_earnings_calendar()
    return set(rows.loc[pd.to_datetime(rows["report_date"]) == pd.Timestamp(D), "ticker"])


def test_a_daily_decision_on_the_report_day_sees_every_timing(lake):
    # decided at the close, filled at the next open: after it in every case
    assert _seen(lake, datetime(2026, 7, 29), Interval.DAY_1) == {"A.US", "B.US", "C.US", "D.US"}


def test_the_day_before_sees_none(lake):
    assert _seen(lake, datetime(2026, 7, 28), Interval.DAY_1) == set()


def test_an_intraday_decision_sees_only_reports_out_by_its_close(lake):
    # the 14:00 hour closes at 15:00 UTC: only the before-the-open report
    assert _seen(lake, datetime(2026, 7, 29, 14), Interval.HOUR_1) == {"A.US"}
    assert _seen(lake, datetime(2026, 7, 29, 21), Interval.HOUR_1) == {"A.US", "B.US"}


def test_the_view_filters_tickers_too(lake):
    view = PointInTimeLake(lake, datetime(2026, 7, 29), decision_interval=Interval.DAY_1)
    assert set(view.get_earnings_calendar(["C.US"])["ticker"]) == {"C.US"}
