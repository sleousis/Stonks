"""Vendor revisions stay point in time (DuckDB 025, principle P12).

Macro prints and fund holdings are revised in place by vendors. Each upsert
keeps a version stamped ``known_at``, so a decision before Stonks saw a
revision reads what was known then, and a holding that drops out of a
re-fetched snapshot leaves it only from the time Stonks saw that."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.store.lake import DuckDBLake
from stonks.store.pit import PointInTimeLake

OBS = date(2023, 12, 31)
FIRST_SEEN = datetime(2024, 1, 10, 12, 0)
REVISED_AT = datetime(2024, 6, 1, 12, 0)


@pytest.fixture
def lake():
    db = DuckDBLake(Path(":memory:"))
    db.migrate()
    yield db
    db.close()


def _macro(value: float, obs: date = OBS) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "country_iso": "USA",
                "indicator": "cpi",
                "observation_date": obs,
                "period": "annual",
                "country_name": "United States",
                "value": value,
            }
        ]
    )


def _values(df: pd.DataFrame) -> list[float]:
    return [float(v) for v in df["value"]]


@pytest.fixture
def revised(lake):
    lake.upsert_macro_indicators(_macro(3.6), known_at=FIRST_SEEN)
    lake.upsert_macro_indicators(_macro(3.7), known_at=REVISED_AT)
    return lake


# ---- macro ---------------------------------------------------------------------


def test_the_current_macro_table_is_the_latest_view(revised):
    assert _values(revised.get_macro_series("USA", "cpi", stamped_at="period_end")) == [3.7]


def test_a_macro_revision_is_invisible_before_it_was_seen(revised):
    before = revised.get_macro_series("USA", "cpi", as_of=date(2024, 3, 1), stamped_at="period_end")
    assert _values(before) == [3.6]
    after = revised.get_macro_series("USA", "cpi", as_of=date(2024, 7, 1), stamped_at="period_end")
    assert _values(after) == [3.7]


def test_the_view_reads_the_macro_version_known_at_the_decision(revised):
    for when, value in ((datetime(2024, 3, 1), 3.6), (datetime(2024, 7, 1), 3.7)):
        view = PointInTimeLake(revised, when, decision_interval=Interval.DAY_1)
        assert _values(view.get_macro_series("USA", "cpi", stamped_at="period_end")) == [value]


def test_the_first_macro_print_counts_whenever_it_was_seen(lake):
    """History ingested late stays usable: the first version of an
    observation goes by its publication date only."""
    lake.upsert_macro_indicators(_macro(3.6), known_at=datetime(2026, 1, 1))
    got = lake.get_macro_series("USA", "cpi", as_of=date(2024, 3, 1), stamped_at="period_end")
    assert _values(got) == [3.6]


def test_the_same_macro_print_again_adds_no_version(revised):
    revised.upsert_macro_indicators(_macro(3.7), known_at=datetime(2024, 9, 1))
    stored = revised.con.execute("SELECT count(*) FROM macro_indicators_versions").fetchone()
    assert stored == (2,)


def test_macro_migration_backfills_existing_rows():
    db = DuckDBLake(Path(":memory:"))
    db.migrate()
    try:
        db.con.execute("DROP TABLE macro_indicators_versions")
        db.con.execute("DROP TABLE fund_holdings_versions")
        db.con.execute("DELETE FROM schema_migrations WHERE version = 25")
        db.con.execute(
            "INSERT INTO macro_indicators VALUES"
            " ('USA', 'cpi', DATE '2023-12-31', 'annual', 'United States', 3.6)"
        )
        db.migrate()
        rows = db.con.execute("SELECT value, known_at FROM macro_indicators_versions").fetchall()
        assert rows == [(3.6, datetime(2023, 12, 31))]
    finally:
        db.close()


# ---- fund holdings -------------------------------------------------------------

SNAP = date(2026, 9, 25)


def _holdings(*rows: tuple[str, float], as_of: date = SNAP) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"fund": "SPY.US", "holding": h, "as_of": as_of, "source": "fake", "weight": w}
            for h, w in rows
        ]
    )


def _read(lake, day: date) -> dict[str, float]:
    df = lake.fund_holdings(["SPY.US"], as_of=day)
    return {str(h): round(float(w), 4) for h, w in zip(df["holding"], df["weight"], strict=True)}


def test_a_holdings_revision_is_invisible_before_it_was_seen(lake):
    lake.upsert_fund_holdings(_holdings(("AAPL.US", 0.07)), known_at=datetime(2026, 9, 26))
    lake.upsert_fund_holdings(_holdings(("AAPL.US", 0.071)), known_at=datetime(2026, 9, 28))
    assert _read(lake, date(2026, 9, 26)) == {"AAPL.US": 0.07}
    assert _read(lake, date(2026, 9, 28)) == {"AAPL.US": 0.071}


def test_a_holding_that_drops_out_leaves_once_known(lake):
    lake.upsert_fund_holdings(
        _holdings(("AAPL.US", 0.07), ("MSFT.US", 0.06)), known_at=datetime(2026, 9, 26)
    )
    lake.upsert_fund_holdings(_holdings(("AAPL.US", 0.07)), known_at=datetime(2026, 9, 28))
    assert _read(lake, date(2026, 9, 27)) == {"AAPL.US": 0.07, "MSFT.US": 0.06}
    assert _read(lake, date(2026, 9, 28)) == {"AAPL.US": 0.07}
    current = lake.con.execute("SELECT holding FROM fund_holdings").fetchall()
    assert current == [("AAPL.US",)]
    # it can come back
    lake.upsert_fund_holdings(
        _holdings(("AAPL.US", 0.07), ("MSFT.US", 0.05)), known_at=datetime(2026, 9, 30)
    )
    assert _read(lake, date(2026, 9, 29)) == {"AAPL.US": 0.07}
    assert _read(lake, date(2026, 9, 30)) == {"AAPL.US": 0.07, "MSFT.US": 0.05}


def test_a_new_snapshot_leaves_older_snapshots_alone(lake):
    lake.upsert_fund_holdings(
        _holdings(("AAPL.US", 0.06), as_of=date(2026, 6, 30)), known_at=datetime(2026, 7, 2)
    )
    lake.upsert_fund_holdings(
        _holdings(("MSFT.US", 0.06), as_of=date(2026, 9, 25)), known_at=datetime(2026, 9, 26)
    )
    old = lake.fund_holdings(["SPY.US"], as_of=date(2026, 7, 2))
    assert list(old["holding"]) == ["AAPL.US"]


def test_the_same_holdings_again_add_no_version(lake):
    for when in (datetime(2026, 9, 26), datetime(2026, 9, 27)):
        lake.upsert_fund_holdings(_holdings(("AAPL.US", 0.07)), known_at=when)
    stored = lake.con.execute("SELECT count(*) FROM fund_holdings_versions").fetchone()
    assert stored == (1,)


def test_holdings_migration_backfills_existing_rows():
    db = DuckDBLake(Path(":memory:"))
    db.migrate()
    try:
        db.con.execute("DROP TABLE macro_indicators_versions")
        db.con.execute("DROP TABLE fund_holdings_versions")
        db.con.execute("DELETE FROM schema_migrations WHERE version = 25")
        db.con.execute(
            "INSERT INTO fund_holdings (fund, holding, as_of, source, weight, known_at)"
            " VALUES ('SPY.US', 'AAPL.US', DATE '2026-09-25', 'fake', 0.07,"
            "         TIMESTAMP '2026-09-26 10:00:00')"
        )
        db.migrate()
        assert _read(db, date(2026, 9, 26)) == {"AAPL.US": 0.07}
        assert db.con.execute("SELECT count(*) FROM fund_holdings_versions").fetchone() == (1,)
    finally:
        db.close()
