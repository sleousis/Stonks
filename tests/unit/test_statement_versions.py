"""Versioned fundamentals (DuckDB 020, principle P12).

Every statement upsert keeps a version stamped with the time Stonks first
saw it (``known_at``). The current tables stay the latest view. A decision
reads the latest version whose known time and filing date are both at or
before it, so a restatement that keeps the original filing date is
invisible before Stonks saw it and visible after."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.store.lake import DuckDBLake
from stonks.store.pit import PitSession, PointInTimeLake
from stonks.store.statement_versions import known_versions

PERIOD = date(2024, 3, 31)
FILED = date(2024, 5, 1)
FIRST_SEEN = datetime(2024, 5, 2, 20, 0)
RESTATED_AT = datetime(2024, 8, 10, 21, 0)
BEFORE = datetime(2024, 8, 9)  # a decision before Stonks saw the restatement
AFTER = datetime(2024, 8, 12)


def _row(revenue: float, **extra) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ticker": "A.US",
                "period_end": PERIOD,
                "frequency": "Q",
                "filing_date": FILED,
                "revenue": revenue,
                **extra,
            }
        ]
    )


@pytest.fixture
def lake():
    db = DuckDBLake(Path(":memory:"))
    db.migrate()
    db.upsert_income_statement(_row(100.0, net_income=10.0), known_at=FIRST_SEEN)
    # the vendor restates revenue and keeps the original filing date
    db.upsert_income_statement(_row(80.0), known_at=RESTATED_AT)
    yield db
    db.close()


def _view(lake, when):
    return PointInTimeLake(lake, when, decision_interval=Interval.DAY_1)


def test_the_current_table_is_the_latest_view(lake):
    current = lake.get_income_statement("A.US")
    assert list(current["revenue"]) == [80.0]
    assert list(current["net_income"]) == [10.0]  # a NULL never overwrites


def test_each_change_is_kept_as_a_version(lake):
    versions = lake.get_statement_versions("income_statement", "A.US")
    assert list(versions["revenue"]) == [100.0, 80.0]
    assert list(versions["net_income"]) == [10.0, 10.0]  # full rows, not deltas
    assert list(pd.to_datetime(versions["known_at"])) == [
        pd.Timestamp(FIRST_SEEN),
        pd.Timestamp(RESTATED_AT),
    ]
    assert list(versions["available_date"]) == [FILED + timedelta(days=1)] * 2


def test_the_same_numbers_again_add_no_version(lake):
    lake.upsert_income_statement(_row(80.0), known_at=AFTER)
    lake.upsert_income_statement(_row(80.0, net_income=10.0))
    assert len(lake.get_statement_versions("income_statement", "A.US")) == 2


def test_a_restatement_after_the_decision_is_invisible_then(lake):
    for read in (
        lambda v: v.get_statement_history("income_statement", "A.US"),
        lambda v: v.get_income_statement("A.US"),
        lambda v: v.get_statements_as_of("income_statement", "A.US", v.as_of),
    ):
        assert list(read(_view(lake, BEFORE))["revenue"]) == [100.0]
        assert list(read(_view(lake, AFTER))["revenue"]) == [80.0]


def test_the_lake_reads_as_of_a_day_pick_the_version_known_then(lake):
    assert list(lake.get_statements_as_of("income_statement", "A.US", BEFORE)["revenue"]) == [100.0]
    assert list(lake.get_statements_as_of("income_statement", "A.US", AFTER)["revenue"]) == [80.0]


def test_the_first_version_counts_from_its_filing(lake):
    """History ingested late is the as-filed version: a backtest of an earlier
    day still sees it once it was filed (and never before)."""
    db = DuckDBLake(Path(":memory:"))
    db.migrate()
    db.upsert_income_statement(_row(100.0))  # first seen today
    try:
        assert list(
            _view(db, BEFORE).get_statement_history("income_statement", "A.US")["revenue"]
        ) == [100.0]
        assert (
            _view(db, datetime(2024, 5, 1)).get_statement_history("income_statement", "A.US").empty
        )
    finally:
        db.close()


def test_a_restated_filing_date_must_have_passed_too(lake):
    lake.upsert_income_statement(
        _row(70.0, filing_date=date(2024, 9, 1)), known_at=datetime(2024, 8, 20)
    )
    view = _view(lake, datetime(2024, 8, 25))
    assert list(view.get_statement_history("income_statement", "A.US")["revenue"]) == [80.0]
    later = _view(lake, datetime(2024, 9, 3))
    assert list(later.get_statement_history("income_statement", "A.US")["revenue"]) == [70.0]


def test_a_view_session_reads_the_versions_once(lake):
    session = PitSession(lake)
    before = session.at(BEFORE, decision_interval=Interval.DAY_1)
    after = session.at(AFTER, decision_interval=Interval.DAY_1)
    assert list(before.get_statement_history("income_statement", "A.US")["revenue"]) == [100.0]
    assert list(after.get_statement_history("income_statement", "A.US")["revenue"]) == [80.0]


def test_the_history_columns_match_the_lake_read(lake):
    view = _view(lake, AFTER)
    history = view.get_statement_history("income_statement", "A.US")
    raw = lake.get_statement_history("income_statement", "A.US")
    pd.testing.assert_frame_equal(history, raw)
    plain = view.get_income_statement("A.US")
    pd.testing.assert_frame_equal(plain, lake.get_income_statement("A.US"))


def test_rows_written_straight_to_a_table_still_read_as_a_first_version(lake):
    lake.con.execute(
        "INSERT INTO balance_sheet (ticker, period_end, frequency, filing_date, total_assets)"
        " VALUES ('A.US', DATE '2024-03-31', 'Q', DATE '2024-05-01', 5.0)"
    )
    view = _view(lake, BEFORE)
    assert list(view.get_statement_history("balance_sheet", "A.US")["total_assets"]) == [5.0]


def test_migration_backfills_existing_rows_as_their_first_version():
    db = DuckDBLake(Path(":memory:"))
    db.migrate()
    try:
        for table in ("income_statement", "balance_sheet", "cash_flow_statement"):
            db.con.execute(f"DROP TABLE {table}_versions")
        db.con.execute("DELETE FROM schema_migrations WHERE version = 20")
        db.con.execute(
            "INSERT INTO income_statement (ticker, period_end, frequency, filing_date, revenue)"
            " VALUES ('A.US', DATE '2024-03-31', 'Q', DATE '2024-05-01', 100.0),"
            "        ('A.US', DATE '2024-06-30', 'Q', NULL, 110.0)"
        )
        db.migrate()
        versions = db.get_statement_versions("income_statement", "A.US")
        assert list(versions["revenue"]) == [100.0, 110.0]
        assert list(pd.to_datetime(versions["known_at"])) == [
            pd.Timestamp("2024-05-01"),
            pd.Timestamp("2024-06-30"),
        ]
        stored = db.con.execute("SELECT count(*) FROM income_statement_versions").fetchone()
        assert stored == (2,)
    finally:
        db.close()


def test_known_versions_picks_per_period():
    versions = pd.DataFrame(
        {
            "ticker": ["A.US"] * 4,
            "period_end": [date(2024, 3, 31)] * 3 + [date(2024, 6, 30)],
            "frequency": ["Q"] * 4,
            "revenue": [1.0, 2.0, 3.0, 4.0],
            "known_at": pd.to_datetime(["2024-06-01", "2024-07-01", "2024-09-01", "2024-10-01"]),
        }
    )
    picked = known_versions(versions, datetime(2024, 8, 1))
    assert list(picked["revenue"]) == [2.0, 4.0]  # the second period's only version
    assert "known_at" not in picked.columns
    filed = pd.Series([True, True, True, False])
    assert list(known_versions(versions, datetime(2024, 8, 1), filed)["revenue"]) == [2.0]
    assert known_versions(versions.iloc[0:0], datetime(2024, 8, 1)).empty
