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


def test_null_drift_is_not_recorded_as_change(lake):
    """I10: a vendor briefly returning NULL for a previously-known value
    is *not* a real change. Recording it would bloat the time series with
    vendor flakiness and dilute the signal. The SCD-2-lite predicate now
    only counts NULL→non-NULL and non-NULL→non-NULL-different as changes;
    non-NULL→NULL is treated as a non-change (vendor temporarily lost
    data).
    """
    # First poll: real value.
    first = pd.DataFrame([_row(date(2026, 1, 1), beta=1.25)])
    lake.upsert_ticker_snapshots(first)
    assert lake.count_rows("ticker_snapshots") == 1

    # Second poll a week later: vendor returned NULL for beta. Must NOT
    # produce a new history row.
    null_row = _row(date(2026, 1, 8))
    null_row["beta"] = None
    df_null = pd.DataFrame([null_row])
    assert lake.upsert_ticker_snapshots(df_null) == 0
    assert lake.count_rows("ticker_snapshots") == 1

    # Third poll: value comes back the same as before. Still no real
    # change vs. the latest stored row, no new history row.
    same = pd.DataFrame([_row(date(2026, 1, 15), beta=1.25)])
    assert lake.upsert_ticker_snapshots(same) == 0
    assert lake.count_rows("ticker_snapshots") == 1

    # Fourth poll: value actually moves. THIS is a real change → insert.
    drifted = pd.DataFrame([_row(date(2026, 1, 22), beta=1.40)])
    assert lake.upsert_ticker_snapshots(drifted) == 1
    assert lake.count_rows("ticker_snapshots") == 2


def test_null_to_value_is_recorded_as_change(lake):
    """The dual of the above: when the previous snapshot had NULL for a
    value column and the new poll provides a real value, that IS a change
    (we just learned the value). Insert a new history row."""
    # First poll: every value column NULL.
    first_row = _row(date(2026, 1, 1))
    for k in ("beta", "short_percent", "percent_insiders", "percent_institutions"):
        first_row[k] = None
    lake.upsert_ticker_snapshots(pd.DataFrame([first_row]))
    assert lake.count_rows("ticker_snapshots") == 1

    # Second poll: vendor now provides beta. We learned something — INSERT.
    second_row = _row(date(2026, 1, 8))
    second_row["beta"] = 1.30
    second_row["short_percent"] = None
    second_row["percent_insiders"] = None
    second_row["percent_institutions"] = None
    assert lake.upsert_ticker_snapshots(pd.DataFrame([second_row])) == 1
    assert lake.count_rows("ticker_snapshots") == 2


def test_transaction_rolls_back_on_exception(lake):
    """The transaction() context manager ROLLBACKs on exception so callers
    can compose multi-statement writes atomically (e.g. _upsert_bundle in
    the pipeline). After a rollback, no rows from the failed block survive.
    """
    # Establish a baseline so we can check the failed block left the table
    # at exactly its original state.
    lake.upsert_ticker_snapshots(pd.DataFrame([_row(date(2026, 1, 1), beta=1.0)]))
    before = lake.count_rows("ticker_snapshots")

    with pytest.raises(RuntimeError, match="boom"), lake.transaction():
        lake.upsert_ticker_snapshots(pd.DataFrame([_row(date(2026, 2, 1), beta=2.0)]))
        raise RuntimeError("boom")  # any exception inside → ROLLBACK

    # The 2026-02-01 row was rolled back; only the original baseline remains.
    assert lake.count_rows("ticker_snapshots") == before


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


# ---- several snapshots in one batch (DS-16) ------------------------------------------


def test_one_batch_of_identical_snapshots_inserts_one_row(lake):
    batch = pd.DataFrame([_row(date(2026, 1, d)) for d in (1, 8, 15)])
    assert lake.upsert_ticker_snapshots(batch) == 1
    assert lake.count_rows("ticker_snapshots") == 1


def test_one_batch_matches_the_same_data_sent_one_poll_at_a_time(tmp_path):
    sequence = [
        _row(date(2026, 1, 1), beta=1.25),
        _row(date(2026, 1, 8), beta=1.25),
        _row(date(2026, 1, 15), beta=1.27),
        _row(date(2026, 1, 22), beta=None),  # a vendor blip, not a change
        _row(date(2026, 1, 29), beta=1.27),
        _row(date(2026, 2, 5), beta=1.25),  # back to an earlier value: a change
    ]
    one_by_one = DuckDBLake(tmp_path / "a.duckdb")
    batched = DuckDBLake(tmp_path / "b.duckdb")
    try:
        for lk in (one_by_one, batched):
            lk.migrate()
        for r in sequence:
            one_by_one.upsert_ticker_snapshots(pd.DataFrame([r]))
        assert batched.upsert_ticker_snapshots(pd.DataFrame(sequence)) == 3
        q = "SELECT CAST(snapshot_date AS VARCHAR) AS d, beta FROM ticker_snapshots ORDER BY 1"
        assert one_by_one.sql(q).to_dict("records") == batched.sql(q).to_dict("records")
    finally:
        one_by_one.close()
        batched.close()


def test_a_batch_newer_than_the_stored_row_compares_against_it(lake):
    lake.upsert_ticker_snapshots(pd.DataFrame([_row(date(2026, 1, 1), beta=1.0)]))
    batch = pd.DataFrame([_row(date(2026, 1, 8), beta=1.0), _row(date(2026, 1, 15), beta=2.0)])
    assert lake.upsert_ticker_snapshots(batch) == 1
