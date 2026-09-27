"""IngestPipeline.run_calendars: each calendar is one unit of soft-fail
accounting under one ``ingest_runs`` row, and re-running is idempotent."""

from __future__ import annotations

from datetime import date

from stonks.calendars.store import CalendarStore
from stonks.ingest.pipeline import IngestPipeline
from stonks.ingest.sources.base import DataSource
from tests.fixtures.calendars import FakeCalendarSource


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
