"""Duplicate primary keys inside one upsert batch must not fail the batch.

DuckDB rejects ``INSERT ... ON CONFLICT DO UPDATE`` when two input rows
hit the same target row ("can not update the same row twice"), which
would lose every row for that ticker. Real case: EODHD's
shares-outstanding ``annual`` and ``quarterly`` lists both carry the
fiscal-year-end date. The lake keeps the *last* row per key.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.store.lake import DuckDBLake


@pytest.fixture
def lake(tmp_path):
    lk = DuckDBLake(tmp_path / "lake.duckdb")
    lk.migrate()
    yield lk
    lk.close()


def _shares(rows):
    return pd.DataFrame(rows, columns=["ticker", "date", "shares"])


def test_upsert_with_duplicate_keys_updates_existing_rows(lake):
    lake.upsert_shares_outstanding(_shares([("AAPL.US", date(2025, 9, 30), 1.0)]))
    lake.upsert_shares_outstanding(
        _shares(
            [
                ("AAPL.US", date(2025, 9, 30), 15_200_000_000.0),
                ("AAPL.US", date(2025, 12, 31), 15_100_000_000.0),
                ("AAPL.US", date(2025, 9, 30), 15_300_000_000.0),
            ]
        )
    )
    got = lake.sql("SELECT date, shares FROM shares_outstanding ORDER BY date")
    assert got["shares"].tolist() == [15_300_000_000.0, 15_100_000_000.0]


def test_upsert_with_duplicate_keys_keeps_last_row_on_first_insert(lake):
    lake.upsert_shares_outstanding(
        _shares(
            [
                ("AAPL.US", date(2025, 9, 30), 1.0),
                ("AAPL.US", date(2025, 9, 30), 2.0),
            ]
        )
    )
    got = lake.sql("SELECT shares FROM shares_outstanding")
    assert got["shares"].tolist() == [2.0]


def _bar(ts, close):
    return {
        "ticker": "X.US",
        "timestamp": ts,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "adj_close": close,
        "volume": 1,
    }


def test_upsert_bars_with_duplicate_keys(lake):
    ts = datetime(2025, 1, 2)
    lake.upsert_bars(pd.DataFrame([_bar(ts, 1.0)]), Interval.DAY_1)
    lake.upsert_bars(pd.DataFrame([_bar(ts, 2.0), _bar(ts, 3.0)]), Interval.DAY_1)
    got = lake.sql("SELECT close FROM bars")
    assert got["close"].tolist() == [3.0]


def test_upsert_statement_with_duplicate_keys(lake):
    row = {"ticker": "AAPL.US", "period_end": date(2025, 9, 30), "frequency": "A"}
    lake.upsert_income_statement(pd.DataFrame([{**row, "revenue": 1.0}]))
    lake.upsert_income_statement(pd.DataFrame([{**row, "revenue": 2.0}, {**row, "revenue": 3.0}]))
    got = lake.get_income_statement("AAPL.US")
    assert got["revenue"].tolist() == [3.0]


def test_upsert_on_change_with_duplicate_keys(lake):
    base = {
        "ticker": "AAPL.US",
        "snapshot_date": date(2025, 1, 1),
        "beta": 1.0,
        "short_percent": None,
        "percent_insiders": None,
        "percent_institutions": None,
    }
    lake.upsert_ticker_snapshots(pd.DataFrame([base]))
    lake.upsert_ticker_snapshots(pd.DataFrame([{**base, "beta": 2.0}, {**base, "beta": 3.0}]))
    got = lake.sql("SELECT beta FROM ticker_snapshots")
    assert got["beta"].tolist() == [3.0]


def test_upsert_officers_with_duplicate_names(lake):
    lake.upsert_officers(
        pd.DataFrame(
            [
                {"ticker": "AAPL.US", "name": "Tim", "title": "CEO", "year_born": 1960},
                {"ticker": "AAPL.US", "name": "Tim", "title": "Chair", "year_born": 1960},
            ]
        )
    )
    got = lake.sql("SELECT title FROM officers")
    assert got["title"].tolist() == ["Chair"]


def test_upsert_insider_with_duplicate_keys(lake):
    row = {
        "ticker": "AAPL.US",
        "transaction_date": date(2025, 1, 2),
        "filing_date": None,
        "owner_name": "Tim",
        "owner_cik": None,
        "owner_relation": None,
        "owner_title": None,
        "transaction_code": "S",
        "acquired_disposed": "D",
        "shares": 100.0,
        "price": 1.0,
        "value": None,
        "post_transaction_amount": None,
        "sec_link": "https://sec/1",
    }
    lake.upsert_insider_transactions(pd.DataFrame([row]))
    lake.upsert_insider_transactions(pd.DataFrame([{**row, "price": 2.0}, {**row, "price": 3.0}]))
    got = lake.sql("SELECT price FROM insider_transactions")
    assert got["price"].tolist() == [3.0]
