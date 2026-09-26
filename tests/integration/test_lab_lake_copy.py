"""copy_universe_lake: an in-memory lake holding every non-bar table a
strategy may read, filtered to the universe."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.lab.lake_copy import copy_universe_lake
from stonks.store.lake import DuckDBLake


@pytest.fixture
def rich_lake(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    con = lake.con
    for t in ("IN.US", "OUT.US"):
        con.execute(
            "INSERT INTO instruments (id, asset_class, sector) VALUES (?, 'equity', 'Tech')", [t]
        )
        con.execute(
            "INSERT INTO income_statement (ticker, period_end, frequency, filing_date, revenue)"
            " VALUES (?, DATE '2025-12-31', 'Q', DATE '2026-02-01', 123.0)",
            [t],
        )
        con.execute(
            "INSERT INTO balance_sheet (ticker, period_end, frequency, total_assets)"
            " VALUES (?, DATE '2025-12-31', 'Q', 9.0)",
            [t],
        )
        con.execute(
            "INSERT INTO dividends (ticker, ex_date, amount) VALUES (?, DATE '2026-01-15', 0.5)",
            [t],
        )
        con.execute(
            "INSERT INTO bars (ticker, timestamp, interval, open, high, low, close, adj_close,"
            " volume) VALUES (?, TIMESTAMP '2026-01-02', '1d', 1, 1, 1, 1, 1, 1)",
            [t],
        )
    con.execute(
        "INSERT INTO macro_indicators (country_iso, indicator, observation_date, value)"
        " VALUES ('USA', 'real_gdp_total', DATE '2025-01-01', 1.5)"
    )
    con.execute(
        "INSERT INTO ingest_runs (id, source, kind, started_at, status)"
        " VALUES (1, 'x', 'prices', TIMESTAMP '2026-01-01', 'ok')"
    )
    yield lake
    lake.close()


def test_copies_universe_rows_of_every_ticker_keyed_table(rich_lake):
    with copy_universe_lake(rich_lake, ["IN.US"]) as copy:
        assert copy.sql("SELECT id FROM instruments")["id"].tolist() == ["IN.US"]
        assert copy.sql("SELECT sector FROM instruments")["sector"].tolist() == ["Tech"]
        stmt = copy.get_income_statement("IN.US")
        assert stmt["revenue"].tolist() == [123.0]
        assert str(stmt["filing_date"].iloc[0])[:10] == str(date(2026, 2, 1))
        assert copy.get_income_statement("OUT.US").empty
        assert copy.count_rows("balance_sheet") == 1
        assert copy.get_dividends("IN.US")["amount"].tolist() == [0.5]
        assert copy.count_rows("dividends") == 1


def test_copies_tables_without_a_ticker_column_whole(rich_lake):
    with copy_universe_lake(rich_lake, ["IN.US"]) as copy:
        assert copy.sql("SELECT value FROM macro_indicators")["value"].tolist() == [1.5]


def test_skips_bars_and_operational_tables(rich_lake):
    with copy_universe_lake(rich_lake, ["IN.US"]) as copy:
        assert copy.count_rows("bars") == 0
        assert copy.count_rows("ingest_runs") == 0


def test_is_a_separate_in_memory_lake_and_source_stays_usable(rich_lake):
    with copy_universe_lake(rich_lake, ["IN.US"]) as copy:
        copy.con.execute("DELETE FROM dividends")
    assert rich_lake.count_rows("dividends") == 2
    assert rich_lake.count_rows("bars") == 2
