"""A canned calendar source for pipeline, service and API tests (no network)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime

from stonks.ingest.calendar_schemas import DividendEventRow, EarningsEventRow, EconomicEventRow
from stonks.ingest.schemas import FinancialStatementsBundle, RawPriceBar
from stonks.ingest.sources.base import DataSource, DataSourceError

DEFAULT_EARNINGS = (
    EarningsEventRow(
        ticker="AAPL.US",
        period_end=date(2026, 9, 30),
        report_date=date(2026, 10, 29),
        before_after_market="after",
    ),
)
DEFAULT_DIVIDENDS = (DividendEventRow(ticker="KO.US", ex_date=date(2026, 10, 14), amount=0.51),)
DEFAULT_ECONOMIC = (
    EconomicEventRow(
        country="US",
        event_time=datetime(2026, 10, 10, 12, 30, tzinfo=UTC),
        event_type="CPI",
        comparison="yoy",
    ),
)


class FakeCalendarSource(DataSource):
    """Canned calendars; records what it was asked for. ``fail`` names
    calendars that raise a soft-fail vendor error."""

    source_id = "fake"

    def __init__(
        self,
        *,
        fail: set[str] | None = None,
        earnings: Sequence[EarningsEventRow] = DEFAULT_EARNINGS,
        dividends: Sequence[DividendEventRow] = DEFAULT_DIVIDENDS,
        economic: Sequence[EconomicEventRow] = DEFAULT_ECONOMIC,
    ) -> None:
        self.fail = fail or set()
        self._earnings = list(earnings)
        self._dividends = list(dividends)
        self._economic = list(economic)
        self.calls: list[tuple[str, date, date, Sequence[str] | None]] = []

    def list_tickers(self, exchange: str) -> list[str]:
        return []

    def fetch_prices(self, ticker, since=None, until=None) -> Iterable[RawPriceBar]:
        return []

    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
        return FinancialStatementsBundle()

    def _check(self, kind: str) -> None:
        if kind in self.fail:
            raise DataSourceError(f"{kind} is down")

    def fetch_earnings_calendar(self, start, end, tickers=None):
        self.calls.append(("earnings", start, end, tickers))
        self._check("earnings")
        return list(self._earnings)

    def fetch_dividend_calendar(self, start, end, tickers=None):
        self.calls.append(("dividends", start, end, tickers))
        self._check("dividends")
        return list(self._dividends)

    def fetch_economic_events(self, start, end, countries=None):
        self.calls.append(("economic", start, end, countries))
        self._check("economic")
        return list(self._economic)
