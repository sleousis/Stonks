"""Guards against whole-table work on hot upsert paths.

These have no functional symptom (results are identical either way), so
the tests spy on the SQL the lake issues.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import pandas as pd

from stonks.core.interval import Interval


class _SpyConnection:
    """Forwards to a real DuckDB connection, recording every SQL string."""

    def __init__(self, con):
        self._con = con
        self.statements: list[str] = []

    def execute(self, query, *args, **kwargs):
        self.statements.append(query)
        return self._con.execute(query, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._con, name)


def _spy(lake) -> _SpyConnection:
    spy = _SpyConnection(lake._con)
    lake._con = spy
    return spy


def _hourly(ticker, n):
    start = datetime(2025, 1, 6)
    return pd.DataFrame(
        [
            {
                "ticker": ticker,
                "timestamp": start + timedelta(hours=i),
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
                "adj_close": 1.5,
                "volume": 10,
            }
            for i in range(n)
        ]
    )


def test_aggregate_bars_does_not_count_whole_bars_table(lake, monkeypatch):
    lake.upsert_bars(_hourly("X.US", 8), Interval.HOUR_1)
    lake.upsert_bars(_hourly("OTHER.US", 8), Interval.HOUR_1)

    def _no_full_count(table):
        raise AssertionError(f"count_rows({table!r}) scans the whole table")

    monkeypatch.setattr(lake, "count_rows", _no_full_count)
    assert lake.aggregate_bars("X.US", Interval.HOUR_1, Interval.HOUR_4) == 2
    # Re-running only updates existing target bars: zero net new rows.
    assert lake.aggregate_bars("X.US", Interval.HOUR_1, Interval.HOUR_4) == 0


def test_statement_column_types_are_cached(lake):
    spy = _spy(lake)
    row = {"ticker": "AAPL.US", "period_end": date(2025, 9, 30), "frequency": "A"}
    for revenue in (1.0, 2.0, 3.0):
        lake.upsert_income_statement(pd.DataFrame([{**row, "revenue": revenue}]))
    schema_lookups = [s for s in spy.statements if "information_schema.columns" in s]
    assert len(schema_lookups) <= 1


def test_column_type_cache_is_reset_by_migrate(lake, tmp_path, monkeypatch):
    lake.upsert_income_statement(
        pd.DataFrame(
            [{"ticker": "A.US", "period_end": date(2025, 9, 30), "frequency": "A", "revenue": 1.0}]
        )
    )
    migs = tmp_path / "migs"
    migs.mkdir()
    (migs / "999_widen.sql").write_text("ALTER TABLE income_statement ADD COLUMN extra DOUBLE;")
    monkeypatch.setattr("stonks.store.lake.MIGRATIONS_DIR", migs)
    lake.migrate()
    assert "extra" in lake._column_types("income_statement")


def test_upsert_on_change_ranks_only_incoming_identities(lake):
    snap = {
        "ticker": "AAPL.US",
        "snapshot_date": date(2025, 1, 1),
        "beta": 1.0,
        "short_percent": None,
        "percent_insiders": None,
        "percent_institutions": None,
    }
    lake.upsert_ticker_snapshots(pd.DataFrame([snap, {**snap, "ticker": "MSFT.US"}]))
    spy = _spy(lake)
    lake.upsert_ticker_snapshots(pd.DataFrame([{**snap, "beta": 2.0}]))
    [stmt] = [s for s in spy.statements if "ROW_NUMBER" in s]
    assert re.search(r"WHERE EXISTS \(SELECT 1 FROM _ids WHERE", stmt), (
        "the latest-row query must be restricted to identities in the batch"
    )
    # Behaviour unchanged: only AAPL's row was updated.
    got = lake.sql("SELECT ticker, beta FROM ticker_snapshots ORDER BY ticker")
    assert got.values.tolist() == [["AAPL.US", 2.0], ["MSFT.US", 1.0]]
