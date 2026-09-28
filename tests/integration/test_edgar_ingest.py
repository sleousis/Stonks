"""EDGAR filings through the pipeline into the lake, and read back point in
time (roadmap 23.13, P12). Hermetic: the recorded fixtures of
tests/unit/test_edgar_source.py."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from stonks.ingest.pipeline import IngestPipeline
from stonks.store.lake import DuckDBLake
from stonks.store.pit import PointInTimeLake
from tests.unit.test_edgar_source import _source


@pytest.fixture
def lake(tmp_path):
    db = DuckDBLake(tmp_path / "lake.duckdb")
    db.migrate()
    yield db
    db.close()


def test_filings_insiders_and_holdings_land_in_the_lake(lake):
    source = _source()
    pipe = IngestPipeline(source, lake)
    lake.upsert_instrument_profile(
        pd.DataFrame([{"id": "AAPL.US", "asset_class": "equity", "cusip": "037833100"}])
    )
    assert pipe.run_filings(["AAPL.US", "NOPE.US"]).tickers_ok == 1
    assert pipe.run_insider_filings(["AAPL.US"], since=date(2026, 9, 1)).status == "ok"
    assert pipe.run_institutional_holdings(["1067983"]).status == "ok"
    # idempotent
    pipe.run_filings(["AAPL.US"])
    pipe.run_institutional_holdings(["1067983"])

    filings = lake.get_corporate_filings(["AAPL.US"])
    assert len(filings) == 6
    assert set(filings["source"]) == {"edgar"}
    earnings = lake.get_corporate_filings(["AAPL.US"], items=["2.02"])
    assert earnings["items"].tolist() == [["2.02", "9.01"]]
    assert lake.get_corporate_filings(forms=["10-Q"])["form"].tolist() == ["10-Q"]
    assert lake.get_corporate_filings(start=date(2026, 9, 1))["form"].tolist() == ["4", "4"]

    trades = lake.get_insider_transactions("AAPL.US")
    assert len(trades) == 1  # the other Form 4 is not recorded: skipped, not fatal
    assert trades["known_at"].notna().all()

    holdings = lake.get_institutional_holdings(ticker="AAPL.US")
    # the March report is not recorded: skipped, not fatal
    assert len(holdings) == 2
    assert set(holdings["report_period"]) == {date(2026, 6, 30)}
    assert len(lake.get_institutional_holdings(cusip="02005N100")) == 1
    assert lake.tickers_by_cusip(["037833100", "nope"]) == {"037833100": "AAPL.US"}


def test_point_in_time_reads_hide_filings_until_accepted(lake):
    pipe = IngestPipeline(_source(), lake)
    pipe.run_filings(["AAPL.US"])
    pipe.run_insider_filings(["AAPL.US"], since=date(2026, 9, 1))
    # the earnings 8-K was accepted at 20:30 UTC on 2026-07-30
    before = PointInTimeLake(lake, datetime(2026, 7, 29))
    on_day = PointInTimeLake(lake, datetime(2026, 7, 30))
    assert "2.02" not in {
        i for items in before.get_corporate_filings(["AAPL.US"])["items"] for i in items
    }
    seen = on_day.get_corporate_filings(["AAPL.US"])
    assert any("2.02" in items for items in seen["items"])
    # the 2026-09-22 trade is hidden until its Form 4 was accepted on 09-24
    trades_at = lambda d: PointInTimeLake(lake, d).get_insider_transactions("AAPL.US")  # noqa: E731
    assert date(2026, 9, 22) not in set(trades_at(datetime(2026, 9, 23))["transaction_date"])
    assert date(2026, 9, 22) in set(trades_at(datetime(2026, 9, 24))["transaction_date"])


def test_insiders_without_acceptance_time_count_from_the_day_after_filing(lake):
    lake.upsert_insider_transactions(
        pd.DataFrame(
            [
                {
                    "ticker": "X.US",
                    "transaction_date": date(2026, 1, 5),
                    "filing_date": date(2026, 1, 7),
                    "owner_name": "A",
                    "owner_cik": None,
                    "owner_relation": "director",
                    "owner_title": None,
                    "transaction_code": "P",
                    "acquired_disposed": "A",
                    "shares": 10.0,
                    "price": 5.0,
                    "value": 50.0,
                    "post_transaction_amount": None,
                    "sec_link": None,
                }
            ]
        )
    )
    seen = lambda d: len(PointInTimeLake(lake, d).get_insider_transactions("X.US"))  # noqa: E731
    assert seen(datetime(2026, 1, 7)) == 0
    assert seen(datetime(2026, 1, 8)) == 1
