"""IngestPipeline.run_calendars: each calendar is one unit of soft-fail
accounting under one ``ingest_runs`` row, and re-running is idempotent."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime

from stonks.calendars.store import CalendarStore
from stonks.ingest.calendar_schemas import DividendEventRow, EarningsEventRow, EconomicEventRow
from stonks.ingest.pipeline import IngestPipeline
from stonks.ingest.schemas import FinancialStatementsBundle, RawPriceBar
from stonks.ingest.sources.base import DataSource, DataSourceError


class FakeCalendarSource(DataSource):
    """Canned calendars; records what it was asked for."""

    source_id = "fake"

    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.fail = fail or set()
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
        return [
            EarningsEventRow(
                ticker="AAPL.US",
                period_end=date(2026, 9, 30),
                report_date=date(2026, 10, 29),
                before_after_market="after",
            )
        ]

    def fetch_dividend_calendar(self, start, end, tickers=None):
        self.calls.append(("dividends", start, end, tickers))
        self._check("dividends")
        return [DividendEventRow(ticker="KO.US", ex_date=date(2026, 10, 14), amount=0.51)]

    def fetch_economic_events(self, start, end, countries=None):
        self.calls.append(("economic", start, end, countries))
        self._check("economic")
        return [
            EconomicEventRow(
                country="US",
                event_time=datetime(2026, 10, 10, 12, 30, tzinfo=UTC),
                event_type="CPI",
                comparison="yoy",
            )
        ]


def test_run_calendars_fills_three_tables_once(lake):
    source = FakeCalendarSource()
    pipeline = IngestPipeline(source=source, lake=lake)
    result = pipeline.run_calendars(date(2026, 10, 1), date(2026, 10, 31))
    assert (result.kind, result.status, result.tickers_ok, result.tickers_failed) == (
        "calendars",
        "ok",
        3,
        0,
    )
    pipeline.run_calendars(date(2026, 10, 1), date(2026, 10, 31))
    store = CalendarStore(lake)
    window = (date(2026, 10, 1), date(2026, 10, 31))
    assert len(store.earnings(*window)) == 1
    assert len(store.dividends(*window)) == 1
    assert len(store.economic(*window)) == 1
    runs = lake.sql("SELECT kind, status FROM ingest_runs ORDER BY id")
    assert runs["kind"].tolist() == ["calendars", "calendars"]


def test_run_calendars_passes_tickers_and_countries(lake):
    source = FakeCalendarSource()
    IngestPipeline(source=source, lake=lake).run_calendars(
        date(2026, 10, 1), date(2026, 10, 31), tickers=["AAPL.US"], countries=["US"]
    )
    assert source.calls == [
        ("earnings", date(2026, 10, 1), date(2026, 10, 31), ["AAPL.US"]),
        ("dividends", date(2026, 10, 1), date(2026, 10, 31), ["AAPL.US"]),
        ("economic", date(2026, 10, 1), date(2026, 10, 31), ["US"]),
    ]


def test_run_calendars_one_kind_failing_is_partial(lake):
    source = FakeCalendarSource(fail={"dividends"})
    result = IngestPipeline(source=source, lake=lake).run_calendars(
        date(2026, 10, 1), date(2026, 10, 31)
    )
    assert result.status == "partial"
    assert result.failed == ("dividends",)
    assert len(CalendarStore(lake).earnings(date(2026, 10, 1), date(2026, 10, 31))) == 1


def test_run_calendars_kinds_subset(lake):
    source = FakeCalendarSource()
    IngestPipeline(source=source, lake=lake).run_calendars(
        date(2026, 10, 1), date(2026, 10, 31), kinds=("earnings",)
    )
    assert [c[0] for c in source.calls] == ["earnings"]


def test_run_calendars_on_a_source_without_calendars_is_an_error_run(lake):
    class NoCalendars(FakeCalendarSource):
        fetch_earnings_calendar = DataSource.fetch_earnings_calendar
        fetch_dividend_calendar = DataSource.fetch_dividend_calendar
        fetch_economic_events = DataSource.fetch_economic_events

    result = IngestPipeline(source=NoCalendars(), lake=lake).run_calendars(
        date(2026, 10, 1), date(2026, 10, 31)
    )
    assert result.status == "error"
    assert result.tickers_failed == 3
