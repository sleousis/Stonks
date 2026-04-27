"""Tests for the SCD-2-lite change-detection helper on DuckDBLake.

The helper backs every ``upsert_*`` method that writes to a snapshot table —
``analyst_ratings``, ``institutional_holders``, ``esg_snapshots``,
``ticker_snapshots``. Behaviour is exercised here against the
``ticker_snapshots`` table specifically because it has the simplest shape
(one identity column + four value columns + a snapshot date), but the
semantics are identical for every consumer of ``_upsert_on_change``.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stonks.store.lake import DuckDBLake


@pytest.fixture
def lake(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    yield lake
    lake.close()


def _row(snapshot: date, *, beta=1.25, short=0.6, ins=7.0, inst=61.0):
    return {
        "ticker": "AAPL.US",
        "snapshot_date": snapshot,
        "beta": beta,
        "short_percent": short,
        "percent_insiders": ins,
        "percent_institutions": inst,
    }


def test_first_insert_adds_one_row(lake):
    df = pd.DataFrame([_row(date(2026, 1, 1))])
    assert lake.upsert_ticker_snapshots(df) == 1
    assert lake.count_rows("ticker_snapshots") == 1


def test_identical_refetch_same_date_is_no_op(lake):
    df = pd.DataFrame([_row(date(2026, 1, 1))])
    lake.upsert_ticker_snapshots(df)
    # Re-insert exactly the same row: ON CONFLICT updates in place; no growth.
    assert lake.upsert_ticker_snapshots(df) == 0
    assert lake.count_rows("ticker_snapshots") == 1


def test_newer_snapshot_with_unchanged_values_is_skipped(lake):
    """The whole point of change-detection: polling more often than the
    underlying data refreshes should not bloat the table."""
    lake.upsert_ticker_snapshots(pd.DataFrame([_row(date(2026, 1, 1))]))
    # Same values, newer snapshot date — must NOT insert.
    same_values = pd.DataFrame([_row(date(2026, 1, 8))])
    assert lake.upsert_ticker_snapshots(same_values) == 0
    assert lake.count_rows("ticker_snapshots") == 1


def test_newer_snapshot_with_changed_value_inserts(lake):
    lake.upsert_ticker_snapshots(pd.DataFrame([_row(date(2026, 1, 1), beta=1.25)]))
    drifted = pd.DataFrame([_row(date(2026, 1, 8), beta=1.30)])
    assert lake.upsert_ticker_snapshots(drifted) == 1
    assert lake.count_rows("ticker_snapshots") == 2
    rows = lake.sql("SELECT snapshot_date, beta FROM ticker_snapshots ORDER BY snapshot_date")
    assert list(rows["beta"]) == [1.25, 1.30]


def test_same_snapshot_with_different_values_updates_in_place(lake):
    """Vendor corrections: same snapshot date, new values — update the row."""
    lake.upsert_ticker_snapshots(pd.DataFrame([_row(date(2026, 1, 1), beta=1.25)]))
    correction = pd.DataFrame([_row(date(2026, 1, 1), beta=1.27)])
    assert lake.upsert_ticker_snapshots(correction) == 0  # no new row
    assert lake.count_rows("ticker_snapshots") == 1
    row = lake.sql("SELECT beta FROM ticker_snapshots").iloc[0]
    assert row["beta"] == 1.27


def test_change_in_any_value_column_triggers_insert(lake):
    """A change in any one of the value columns (not just beta) must
    register as a meaningful change."""
    base = pd.DataFrame([_row(date(2026, 1, 1))])
    lake.upsert_ticker_snapshots(base)
    # Only short_percent moved.
    moved = pd.DataFrame([_row(date(2026, 1, 8), short=1.1)])
    assert lake.upsert_ticker_snapshots(moved) == 1
    assert lake.count_rows("ticker_snapshots") == 2


def test_distinct_identities_dont_interfere(lake):
    """Different ``ticker`` values are different entities; one ticker's
    history doesn't suppress inserts for another ticker."""
    a = pd.DataFrame([_row(date(2026, 1, 1))])
    a["ticker"] = "AAPL.US"
    b = a.copy()
    b["ticker"] = "MSFT.US"
    lake.upsert_ticker_snapshots(a)
    assert lake.upsert_ticker_snapshots(b) == 1
    assert lake.count_rows("ticker_snapshots") == 2


def test_full_sequence_idempotent_then_drift(lake):
    """End-to-end: 4 polls, only the genuine drift should grow the table."""
    sequence = [
        _row(date(2026, 1, 1), beta=1.25),
        _row(date(2026, 1, 8), beta=1.25),  # no change → skip
        _row(date(2026, 1, 15), beta=1.27),  # drift → insert
        _row(date(2026, 1, 22), beta=1.27),  # no change → skip
    ]
    for r in sequence:
        lake.upsert_ticker_snapshots(pd.DataFrame([r]))
    assert lake.count_rows("ticker_snapshots") == 2
    rows = lake.sql(
        "SELECT CAST(snapshot_date AS VARCHAR) AS d FROM ticker_snapshots ORDER BY snapshot_date"
    )
    # Only the dates where data actually changed are stored.
    assert list(rows["d"]) == ["2026-01-01", "2026-01-15"]
