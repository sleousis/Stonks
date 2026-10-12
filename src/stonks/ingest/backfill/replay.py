"""A :class:`DataSource` that serves rows the backfill already fetched, so
the write path (validation, quarantine, soft failure, ``ingest_runs``)
is the ingest pipeline's own."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from stonks.core.interval import Interval
from stonks.ingest.metadata_bundle import CompanyData, CompanyPart
from stonks.ingest.reference_schemas import (
    ExchangeDetailsRow,
    IdentifierPage,
    IndexComponentsRow,
    TreasuryCurve,
)
from stonks.ingest.schemas import FinancialStatementsBundle
from stonks.ingest.sources.base import DataSource, DataSourceError


class ReplaySource(DataSource):
    """Answers each fetch with what ``results`` holds for its key (an
    exception is raised again). The key is the ticker, ``COUNTRY:indicator``
    for macro, ``BASEQUOTE`` for FX, the calendar's name for calendars, the
    index code, the exchange code, ``curve:year`` for Treasury rates,
    ``EXCHANGE:offset`` for identifiers, ``filings:``, ``insiders:`` or
    ``holdings:`` before a ticker or filer, the market for borrow rates and
    the chain for TVL."""

    def __init__(self, source_id: str, results: Mapping[str, Any]) -> None:
        self.source_id = source_id
        self._results = results

    def _get(self, ticker: str) -> Any:
        if ticker not in self._results:
            raise DataSourceError(f"{ticker} was not fetched")
        value = self._results[ticker]
        if isinstance(value, BaseException):
            raise value
        return value

    def list_tickers(self, exchange: str) -> list[str]:
        raise DataSourceError("not available while replaying fetched rows")

    def fetch_prices(
        self, ticker: str, since: date | None = None, until: date | None = None
    ) -> Iterable[Any]:
        return self._get(ticker)

    def fetch_intraday_bars(
        self,
        ticker: str,
        interval: Interval,
        since: date | None = None,
        until: date | None = None,
    ) -> Iterable[Any]:
        return self._get(ticker)

    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
        raise DataSourceError("not available while replaying fetched rows")

    def fetch_company_data(self, ticker: str, parts: Collection[CompanyPart]) -> CompanyData:
        return self._get(ticker)

    def fetch_macro_indicator(self, country_iso: str, indicator: str) -> Iterable[Any]:
        return self._get(f"{country_iso}:{indicator}")

    def fetch_fx_rates(
        self, base: str, quote: str, since: date | None = None, until: date | None = None
    ) -> Iterable[Any]:
        return self._get(f"{base}{quote}")

    def fetch_earnings_calendar(
        self, start: date, end: date, tickers: Sequence[str] | None = None
    ) -> Iterable[Any]:
        return self._get("earnings")

    def fetch_dividend_calendar(
        self, start: date, end: date, tickers: Sequence[str] | None = None
    ) -> Iterable[Any]:
        return self._get("dividends")

    def fetch_economic_events(
        self, start: date, end: date, countries: Sequence[str] | None = None
    ) -> Iterable[Any]:
        return self._get("economic")

    def fetch_index_components(self, index_code: str, as_of: date) -> IndexComponentsRow:
        return self._get(index_code)

    def fetch_exchange_details(self, exchange: str, start: date, end: date) -> ExchangeDetailsRow:
        return self._get(exchange)

    def fetch_treasury_rates(self, curve: TreasuryCurve, year: int) -> Iterable[Any]:
        return self._get(f"{curve}:{year}")

    def fetch_ipo_calendar(self, start: date, end: date) -> Iterable[Any]:
        return self._get("ipos")

    def fetch_split_calendar(self, start: date, end: date) -> Iterable[Any]:
        return self._get("splits")

    def fetch_identifiers(self, exchange: str, offset: int, limit: int) -> IdentifierPage:
        return self._get(f"{exchange}:{offset}")

    def fetch_filings(
        self,
        ticker: str,
        since: date | None = None,
        until: date | None = None,
        forms: Sequence[str] | None = None,
    ) -> Iterable[Any]:
        return self._get(f"filings:{ticker}")

    def fetch_insider_filings(
        self, ticker: str, since: date | None = None, until: date | None = None
    ) -> Iterable[Any]:
        return self._get(f"insiders:{ticker}")

    def fetch_institutional_holdings(
        self, filer_cik: str, since: date | None = None, until: date | None = None
    ) -> Iterable[Any]:
        return self._get(f"holdings:{filer_cik}")

    def fetch_borrow_rates(self, market: str) -> Iterable[Any]:
        return self._get(market)

    def fetch_chain_tvl(self, chain: str, since: date | None = None) -> Iterable[Any]:
        return self._get(chain)
