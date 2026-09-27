"""CalendarStore: idempotent upserts and range reads of the event
calendars (lake migration 021), plus news reads."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd

from stonks.calendars.store import CalendarStore
from stonks.ingest.calendar_schemas import DividendEventRow, EarningsEventRow, EconomicEventRow


def _earn(ticker: str, period: date, report: date, **kw) -> EarningsEventRow:
    return EarningsEventRow(ticker=ticker, period_end=period, report_date=report, **kw)


def test_migration_creates_calendar_tables(lake):
    tables = set(lake.tables())
    assert {"earnings_calendar", "dividend_calendar", "economic_events", "screens"} <= tables


def test_earnings_upsert_is_idempotent_and_moves_report_date(lake):
    store = CalendarStore(lake)
    rows = [
        _earn("AAPL.US", date(2026, 9, 30), date(2026, 10, 29), before_after_market="after"),
        _earn("MSFT.US", date(2026, 9, 30), date(2026, 10, 27), eps_estimate=3.1),
    ]
    assert store.upsert_earnings(rows, source="fake") == 2
    assert store.upsert_earnings(rows, source="fake") == 2
    # the company moves its date: same period, one row
    store.upsert_earnings(
        [_earn("AAPL.US", date(2026, 9, 30), date(2026, 10, 30), before_after_market="after")],
        source="fake",
    )
    got = store.earnings(date(2026, 10, 1), date(2026, 10, 31))
    assert [(e.ticker, e.report_date) for e in got] == [
        ("MSFT.US", date(2026, 10, 27)),
        ("AAPL.US", date(2026, 10, 30)),
    ]
    assert got[1].before_after_market == "after"
    assert got[0].eps_estimate == 3.1


def test_earnings_filter_by_tickers_and_window(lake):
    store = CalendarStore(lake)
    store.upsert_earnings(
        [
            _earn("A.US", date(2026, 6, 30), date(2026, 7, 20)),
            _earn("B.US", date(2026, 6, 30), date(2026, 7, 22)),
            _earn("A.US", date(2026, 9, 30), date(2026, 10, 20)),
        ],
        source="fake",
    )
    got = store.earnings(date(2026, 7, 1), date(2026, 7, 31), tickers=["A.US"])
    assert [(e.ticker, e.report_date) for e in got] == [("A.US", date(2026, 7, 20))]
    assert store.earnings(date(2026, 7, 1), date(2026, 7, 31), tickers=[]) == []


def test_dividend_upsert_keeps_amount_when_later_row_has_none(lake):
    store = CalendarStore(lake)
    store.upsert_dividends(
        [DividendEventRow(ticker="KO.US", ex_date=date(2026, 11, 28), amount=0.51)],
        source="fake",
    )
    store.upsert_dividends(
        [DividendEventRow(ticker="KO.US", ex_date=date(2026, 11, 28))], source="fake"
    )
    got = store.dividends(date(2026, 11, 1), date(2026, 11, 30))
    assert len(got) == 1
    assert got[0].amount == 0.51


def test_dividend_amount_falls_back_to_the_dividends_table(lake):
    store = CalendarStore(lake)
    store.upsert_dividends(
        [DividendEventRow(ticker="KO.US", ex_date=date(2026, 11, 28))], source="fake"
    )
    lake.upsert_dividends(
        pd.DataFrame(
            [
                {
                    "ticker": "KO.US",
                    "ex_date": date(2026, 11, 28),
                    "amount": 0.52,
                    "currency": "USD",
                    "pay_date": date(2026, 12, 15),
                    "record_date": None,
                    "declaration_date": None,
                }
            ]
        )
    )
    [event] = store.dividends(date(2026, 11, 1), date(2026, 11, 30))
    assert event.amount == 0.52
    assert event.currency == "USD"
    assert event.pay_date == date(2026, 12, 15)


def test_economic_events_by_country(lake):
    store = CalendarStore(lake)
    t = datetime(2026, 10, 10, 12, 30, tzinfo=UTC)
    rows = [
        EconomicEventRow(country="us", event_time=t, event_type="CPI", comparison="yoy"),
        EconomicEventRow(country="US", event_time=t, event_type="CPI", comparison="mom"),
        EconomicEventRow(country="DE", event_time=t, event_type="ZEW", estimate=10.0),
    ]
    assert store.upsert_economic(rows, source="fake") == 3
    store.upsert_economic(rows, source="fake")
    all_events = store.economic(date(2026, 10, 1), date(2026, 10, 31))
    assert len(all_events) == 3
    us = store.economic(date(2026, 10, 1), date(2026, 10, 31), countries=["US"])
    assert {(e.event_type, e.comparison) for e in us} == {("CPI", "yoy"), ("CPI", "mom")}
    assert us[0].event_time.tzinfo is not None


def test_news_and_sentiment_reads(lake):
    lake.upsert_news(
        pd.DataFrame(
            [
                {
                    "ticker": "AAPL.US",
                    "published_at": datetime(2026, 9, 20, 14, 0),
                    "title": "Apple ships",
                    "url": "https://example.com/a",
                    "source_name": "Wire",
                    "content": None,
                    "symbols": ["AAPL.US"],
                    "tags": ["tech"],
                    "sentiment": 0.6,
                    "sentiment_pos": 0.7,
                    "sentiment_neg": 0.1,
                    "sentiment_neu": 0.2,
                },
                {
                    "ticker": "MSFT.US",
                    "published_at": datetime(2026, 9, 21, 9, 0),
                    "title": "Microsoft slips",
                    "url": None,
                    "source_name": None,
                    "content": None,
                    "symbols": [],
                    "tags": [],
                    "sentiment": -0.4,
                    "sentiment_pos": None,
                    "sentiment_neg": None,
                    "sentiment_neu": None,
                },
            ]
        )
    )
    lake.upsert_news_sentiment(
        pd.DataFrame(
            [
                {
                    "ticker": "AAPL.US",
                    "date": date(2026, 9, 20),
                    "sentiment": 0.5,
                    "article_count": 3,
                },
                {
                    "ticker": "AAPL.US",
                    "date": date(2026, 9, 21),
                    "sentiment": 0.2,
                    "article_count": 1,
                },
            ]
        )
    )
    store = CalendarStore(lake)
    news = store.news(["AAPL.US", "MSFT.US"], limit=10)
    assert [n.title for n in news] == ["Microsoft slips", "Apple ships"]
    assert news[1].tags == ["tech"]
    assert news[1].url == "https://example.com/a"
    assert store.news(["AAPL.US"], limit=10, since=date(2026, 9, 21)) == []
    assert store.news([], limit=10) == []
    sentiment = store.sentiment(["AAPL.US"], since=date(2026, 9, 1))
    assert [(s.day, s.sentiment, s.article_count) for s in sentiment] == [
        (date(2026, 9, 20), 0.5, 3),
        (date(2026, 9, 21), 0.2, 1),
    ]
