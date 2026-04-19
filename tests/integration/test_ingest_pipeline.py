"""Integration tests for the IngestPipeline using a FakeDataSource.

No network; all vendor IO is mocked at the DataSource boundary."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import pytest

from stonks.ingest.pipeline import IngestPipeline, IngestRunResult
from stonks.ingest.schemas import FundamentalRow, RawPriceBar
from stonks.ingest.sources.base import DataSource
from stonks.store.lake import DuckDBLake


class FakeDataSource(DataSource):
    source_id = "fake"

    def __init__(
        self,
        prices: dict[str, list[RawPriceBar]] | None = None,
        fundamentals: dict[str, list[FundamentalRow]] | None = None,
        fail_on: set[str] | None = None,
    ):
        self._prices = prices or {}
        self._fundamentals = fundamentals or {}
        self._fail_on = fail_on or set()
        self.price_calls: list[str] = []
        self.fundamental_calls: list[str] = []

    def list_tickers(self, exchange: str) -> list[str]:
        return sorted(self._prices.keys())

    def fetch_prices(
        self, ticker: str, since: date | None = None, until: date | None = None
    ) -> Iterable[RawPriceBar]:
        self.price_calls.append(ticker)
        if ticker in self._fail_on:
            raise RuntimeError(f"boom on {ticker}")
        return list(self._prices.get(ticker, []))

    def fetch_fundamentals(self, ticker: str) -> list[FundamentalRow]:
        self.fundamental_calls.append(ticker)
        if ticker in self._fail_on:
            raise RuntimeError(f"boom on {ticker}")
        return list(self._fundamentals.get(ticker, []))


def _bar(ticker: str, d: date, close: float = 100.0) -> RawPriceBar:
    return RawPriceBar(
        ticker=ticker,
        date=d,
        open=close - 1.0,
        high=close + 1.0,
        low=close - 2.0,
        close=close,
        adj_close=close,
        volume=1_000_000,
    )


def _fund(ticker: str, line_item: str, value: float) -> FundamentalRow:
    return FundamentalRow(
        ticker=ticker,
        period_end=date(2025, 12, 31),
        frequency="Q",
        statement="income",
        line_item=line_item,
        value=value,
    )


@pytest.fixture
def lake(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    yield lake
    lake.close()


def test_run_prices_happy_path(lake):
    src = FakeDataSource(
        prices={
            "AAPL.US": [_bar("AAPL.US", date(2026, 4, 1)), _bar("AAPL.US", date(2026, 4, 2))],
            "MSFT.US": [_bar("MSFT.US", date(2026, 4, 1))],
        }
    )
    pipe = IngestPipeline(source=src, lake=lake)

    result = pipe.run_prices(["AAPL.US", "MSFT.US"], since=date(2026, 4, 1))

    assert isinstance(result, IngestRunResult)
    assert result.status == "ok"
    assert result.tickers_ok == 2
    assert result.tickers_failed == 0
    assert lake.count_rows("prices") == 3

    runs = lake.sql("SELECT * FROM ingest_runs ORDER BY id DESC LIMIT 1")
    assert runs.iloc[0]["status"] == "ok"
    assert runs.iloc[0]["tickers_ok"] == 2
    assert runs.iloc[0]["kind"] == "prices"


def test_run_prices_soft_fails_on_bad_ticker(lake):
    src = FakeDataSource(
        prices={
            "AAPL.US": [_bar("AAPL.US", date(2026, 4, 1))],
            "BAD.US": [],
        },
        fail_on={"BAD.US"},
    )
    pipe = IngestPipeline(source=src, lake=lake)

    result = pipe.run_prices(["AAPL.US", "BAD.US"], since=date(2026, 4, 1))

    assert result.status == "partial"
    assert result.tickers_ok == 1
    assert result.tickers_failed == 1
    assert lake.count_rows("prices") == 1  # good ticker still wrote
    assert src.price_calls == ["AAPL.US", "BAD.US"]  # didn't bail out after error


def test_run_prices_all_fail_is_error(lake):
    src = FakeDataSource(
        prices={"A.US": [], "B.US": []},
        fail_on={"A.US", "B.US"},
    )
    pipe = IngestPipeline(source=src, lake=lake)

    result = pipe.run_prices(["A.US", "B.US"], since=date(2026, 4, 1))

    assert result.status == "error"
    assert result.tickers_ok == 0
    assert result.tickers_failed == 2


def test_run_prices_is_idempotent(lake):
    src = FakeDataSource(prices={"AAPL.US": [_bar("AAPL.US", date(2026, 4, 1))]})
    pipe = IngestPipeline(source=src, lake=lake)
    pipe.run_prices(["AAPL.US"], since=date(2026, 4, 1))
    pipe.run_prices(["AAPL.US"], since=date(2026, 4, 1))
    assert lake.count_rows("prices") == 1  # upsert, not duplicate


def test_run_prices_with_empty_ticker_list_records_ok_noop(lake):
    src = FakeDataSource()
    pipe = IngestPipeline(source=src, lake=lake)
    result = pipe.run_prices([], since=date(2026, 4, 1))
    assert result.status == "ok"
    assert result.tickers_ok == 0
    assert result.tickers_failed == 0


def test_run_fundamentals_happy_path(lake):
    src = FakeDataSource(
        fundamentals={
            "AAPL.US": [_fund("AAPL.US", "totalRevenue", 123.0), _fund("AAPL.US", "netIncome", 45.0)],
        }
    )
    pipe = IngestPipeline(source=src, lake=lake)

    result = pipe.run_fundamentals(["AAPL.US"])
    assert result.status == "ok"
    assert result.tickers_ok == 1
    assert lake.count_rows("fundamentals") == 2
