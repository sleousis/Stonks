"""Vendor-neutral rows for the event calendars (roadmap 20.7, lake
migration 021).

Every :class:`~stonks.ingest.sources.base.DataSource` that serves calendars
maps its vendor JSON into these rows at parse time, so the lake tables never
carry vendor field names or vendor vocabulary.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field, field_validator

from stonks.ingest.schemas import FrozenRow

#: When a company reports relative to its session. Adapters map vendor
#: strings ("BeforeMarket", "AMC", ...) into these.
ReportTiming = Literal["before", "during", "after"]

#: What an economic figure is compared with. ``none`` when the vendor
#: gives no comparison (a level such as a rate decision).
Comparison = Literal["mom", "qoq", "yoy", "none"]


class EarningsEventRow(FrozenRow):
    """One company's earnings report for one fiscal period. Upcoming reports
    carry an estimate and no actual; past ones carry both."""

    ticker: str = Field(min_length=1)
    period_end: date
    report_date: date
    before_after_market: ReportTiming | None = None
    currency: str | None = None
    eps_estimate: float | None = None
    eps_actual: float | None = None
    eps_difference: float | None = None
    surprise_percent: float | None = None


class DividendEventRow(FrozenRow):
    """One ex-dividend date. Some vendors' calendars list only the date, so
    the amount and the other dates are optional."""

    ticker: str = Field(min_length=1)
    ex_date: date
    amount: float | None = Field(default=None, ge=0)
    currency: str | None = None
    record_date: date | None = None
    pay_date: date | None = None
    declaration_date: date | None = None


class EconomicEventRow(FrozenRow):
    """One scheduled macro release. ``country`` is ISO 3166-1 alpha-2 or a
    region code (``EU``), upper case. ``event_time`` is UTC."""

    country: str = Field(min_length=2, max_length=3)
    event_time: datetime
    event_type: str = Field(min_length=1, max_length=200)
    comparison: Comparison = "none"
    period: str | None = None
    actual: float | None = None
    previous: float | None = None
    estimate: float | None = None
    change: float | None = None
    change_pct: float | None = None

    @field_validator("country")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()
