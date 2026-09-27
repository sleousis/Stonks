"""Integration tests for the IngestPipeline using a FakeDataSource.

No network; all vendor IO is mocked at the DataSource boundary."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import pytest

from stonks.ingest.pipeline import IngestPipeline, IngestRunResult
from stonks.ingest.schemas import (
    BalanceSheetRow,
    CashFlowStatementRow,
    FinancialStatementsBundle,
    IncomeStatementRow,
    RawPriceBar,
)
from stonks.ingest.sources.base import DataSource, DataSourceError


class FakeDataSource(DataSource):
    source_id = "fake"

    def __init__(
        self,
        prices: dict[str, list[RawPriceBar]] | None = None,
        fundamentals: dict[str, FinancialStatementsBundle] | None = None,
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
            # DataSourceError is what the pipeline soft-fails on; bare
            # RuntimeError would propagate as a programmer-bug signal now.
            raise DataSourceError(f"boom on {ticker}")
        return list(self._prices.get(ticker, []))

    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
        self.fundamental_calls.append(ticker)
        if ticker in self._fail_on:
            # DataSourceError is what the pipeline soft-fails on; bare
            # RuntimeError would propagate as a programmer-bug signal now.
            raise DataSourceError(f"boom on {ticker}")
        return self._fundamentals.get(ticker, FinancialStatementsBundle())


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


def _income(ticker: str, **values: float) -> IncomeStatementRow:
    return IncomeStatementRow(
        ticker=ticker,
        period_end=date(2025, 12, 31),
        frequency="Q",
        **values,
    )


def _balance(ticker: str, **values: float) -> BalanceSheetRow:
    return BalanceSheetRow(
        ticker=ticker,
        period_end=date(2025, 12, 31),
        frequency="Q",
        **values,
    )


def _cashflow(ticker: str, **values: float) -> CashFlowStatementRow:
    return CashFlowStatementRow(
        ticker=ticker,
        period_end=date(2025, 12, 31),
        frequency="Q",
        **values,
    )


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
            "AAPL.US": FinancialStatementsBundle(
                income=(_income("AAPL.US", revenue=123.0, net_income=45.0),),
                balance=(_balance("AAPL.US", total_assets=500.0),),
                cashflow=(_cashflow("AAPL.US", operating_cash_flow=80.0),),
            ),
        }
    )
    pipe = IngestPipeline(source=src, lake=lake)

    result = pipe.run_fundamentals(["AAPL.US"])
    assert result.status == "ok"
    assert result.tickers_ok == 1
    assert lake.count_rows("income_statement") == 1
    assert lake.count_rows("balance_sheet") == 1
    assert lake.count_rows("cash_flow_statement") == 1


def test_run_fundamentals_partial_bundle_only_writes_present_statements(lake):
    src = FakeDataSource(
        fundamentals={
            "MSFT.US": FinancialStatementsBundle(
                income=(_income("MSFT.US", revenue=200.0),),
                # balance + cashflow intentionally empty
            ),
        }
    )
    pipe = IngestPipeline(source=src, lake=lake)
    result = pipe.run_fundamentals(["MSFT.US"])
    assert result.status == "ok"
    assert lake.count_rows("income_statement") == 1
    assert lake.count_rows("balance_sheet") == 0
    assert lake.count_rows("cash_flow_statement") == 0


def test_run_fundamentals_soft_fails_on_bad_ticker(lake):
    src = FakeDataSource(
        fundamentals={
            "GOOD.US": FinancialStatementsBundle(
                income=(_income("GOOD.US", revenue=10.0),),
            ),
        },
        fail_on={"BAD.US"},
    )
    pipe = IngestPipeline(source=src, lake=lake)
    result = pipe.run_fundamentals(["GOOD.US", "BAD.US"])
    assert result.status == "partial"
    assert result.tickers_ok == 1
    assert result.tickers_failed == 1
    assert lake.count_rows("income_statement") == 1


def test_run_fundamentals_is_idempotent(lake):
    bundle = FinancialStatementsBundle(
        income=(_income("AAPL.US", revenue=123.0),),
        balance=(_balance("AAPL.US", total_assets=500.0),),
    )
    src = FakeDataSource(fundamentals={"AAPL.US": bundle})
    pipe = IngestPipeline(source=src, lake=lake)
    pipe.run_fundamentals(["AAPL.US"])
    pipe.run_fundamentals(["AAPL.US"])
    assert lake.count_rows("income_statement") == 1
    assert lake.count_rows("balance_sheet") == 1


def test_run_fundamentals_closes_run_row_when_unhandled_exception_escapes(lake):
    """An unhandled exception (programmer bug, KeyboardInterrupt) must
    still close the ``ingest_runs`` row so operators don't have to
    triage orphaned ``running`` rows by hand."""

    class _ExplodingSource(FakeDataSource):
        def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
            raise RuntimeError("synthetic programmer bug")

    pipe = IngestPipeline(source=_ExplodingSource(), lake=lake)
    with pytest.raises(RuntimeError, match="synthetic programmer bug"):
        pipe.run_fundamentals(["AAPL.US"])
    runs = lake.sql("SELECT status, finished_at, error FROM ingest_runs ORDER BY id DESC LIMIT 1")
    assert runs.iloc[0]["status"] == "error"
    assert runs.iloc[0]["finished_at"] is not None
    assert "synthetic programmer bug" in runs.iloc[0]["error"]


class _AlwaysRaisingSource(FakeDataSource):
    """Every fetch raises ``self.exc`` — used to drive each run type into
    its unhandled-exception (or soft-fail) path."""

    def __init__(self, exc: BaseException):
        super().__init__()
        self.exc = exc

    def fetch_prices(self, ticker, since=None, until=None):
        raise self.exc

    def fetch_fundamentals(self, ticker):
        raise self.exc

    def fetch_metadata(self, ticker):
        raise self.exc

    def fetch_intraday_bars(self, ticker, interval, since=None, until=None):
        raise self.exc

    def fetch_macro_indicator(self, country_iso, indicator):
        raise self.exc


def _invoke(pipe: IngestPipeline, kind: str):
    from stonks.core.interval import Interval

    if kind == "prices":
        return pipe.run_prices(["AAPL.US"])
    if kind == "fundamentals":
        return pipe.run_fundamentals(["AAPL.US"])
    if kind == "metadata":
        return pipe.run_metadata(["AAPL.US"])
    if kind == "intraday":
        return pipe.run_intraday_bars(["AAPL.US"], interval=Interval.HOUR_1)
    if kind == "macro":
        return pipe.run_macro_indicators(["USA"], ["real_gdp_total"])
    raise AssertionError(kind)


_RUN_KINDS = ["prices", "fundamentals", "metadata", "intraday", "macro"]


@pytest.mark.parametrize("kind", _RUN_KINDS)
@pytest.mark.parametrize(
    "exc",
    [KeyError("malformed row"), KeyboardInterrupt()],
    ids=["keyerror", "ctrl-c"],
)
def test_every_run_type_closes_run_row_on_unhandled_exception(lake, kind, exc):
    pipe = IngestPipeline(source=_AlwaysRaisingSource(exc), lake=lake)
    with pytest.raises(type(exc)):
        _invoke(pipe, kind)
    runs = lake.sql("SELECT status, finished_at, error FROM ingest_runs ORDER BY id DESC LIMIT 1")
    assert runs.iloc[0]["status"] == "error"
    assert runs.iloc[0]["finished_at"] is not None
    assert type(exc).__name__ in runs.iloc[0]["error"]


@pytest.mark.parametrize("kind", _RUN_KINDS)
def test_every_run_type_redacts_credentials_from_stored_and_logged_errors(lake, kind, capsys):
    secret = "LeakyToken987"
    exc = DataSourceError(f"404 for url: https://vendor/api/x?fmt=json&api_token={secret}")
    pipe = IngestPipeline(source=_AlwaysRaisingSource(exc), lake=lake)
    result = _invoke(pipe, kind)
    assert result.status == "error"
    runs = lake.sql("SELECT error FROM ingest_runs ORDER BY id DESC LIMIT 1")
    assert "api_token=***" in runs.iloc[0]["error"]
    assert secret not in runs.iloc[0]["error"]
    assert secret not in capsys.readouterr().out


def test_eodhd_http_error_never_reaches_ingest_runs_with_api_key(lake, monkeypatch):
    import requests

    from stonks.ingest.sources import eodhd

    secret = "RealEodhdKey555"

    class _Resp:
        status_code = 404
        text = "not found"

        def __init__(self, url):
            self.url = url

        def raise_for_status(self):
            raise requests.HTTPError(f"404 Client Error for url: {self.url}", response=self)

    class _Session:
        def get(self, url, params=None, timeout=None):
            query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
            return _Resp(f"{url}?{query}")

    monkeypatch.setattr(eodhd.time, "sleep", lambda s: None)
    source = eodhd.EodhdDataSource(api_key=secret, session=_Session())  # type: ignore[arg-type]
    result = IngestPipeline(source=source, lake=lake).run_prices(["AAPL.US"])
    assert result.status == "error"
    error = lake.sql("SELECT error FROM ingest_runs ORDER BY id DESC LIMIT 1").iloc[0]["error"]
    assert "HTTPError" in error
    assert secret not in error
