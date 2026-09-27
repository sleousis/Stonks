"""What the calendar and news reads return. Plain pydantic models: the API
serves them as they are."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel

from stonks.ingest.calendar_schemas import Comparison, ReportTiming


class EarningsEvent(BaseModel):
    ticker: str
    #: The instrument's name when the lake knows it.
    name: str | None = None
    period_end: date
    report_date: date
    before_after_market: ReportTiming | None = None
    currency: str | None = None
    eps_estimate: float | None = None
    eps_actual: float | None = None
    eps_difference: float | None = None
    surprise_percent: float | None = None


class DividendEvent(BaseModel):
    ticker: str
    name: str | None = None
    ex_date: date
    amount: float | None = None
    currency: str | None = None
    record_date: date | None = None
    pay_date: date | None = None
    declaration_date: date | None = None


class EconomicEvent(BaseModel):
    country: str
    event_time: datetime
    event_type: str
    comparison: Comparison
    period: str | None = None
    actual: float | None = None
    previous: float | None = None
    estimate: float | None = None
    change: float | None = None
    change_pct: float | None = None


class NewsItem(BaseModel):
    ticker: str
    published_at: datetime
    title: str
    url: str | None = None
    source_name: str | None = None
    #: Polarity from -1 (negative) to 1 (positive), when the vendor scores it.
    sentiment: float | None = None
    tags: list[str] = []


class SentimentDay(BaseModel):
    ticker: str
    day: date
    sentiment: float | None = None
    article_count: int | None = None
