"""Canonical ingestion row schemas.

These are the *normalized* rows that flow from any `DataSource` into the lake —
vendor-specific shapes are translated up-front, so the pipeline and the lake
never see vendor fields.

Schemas cover the full fundamentals surface area (dividends, insider trades,
news, analyst data, shares outstanding, employee count, segmentations,
profile) — not just the three financial statements — so strategies can draw
on whatever they need without each layer having to be extended.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ---- prices + financial statements (original surface) ----------------------


class RawPriceBar(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    date: date
    open: float
    high: float
    low: float
    close: float
    adj_close: float
    volume: int | None = None


class FundamentalRow(BaseModel):
    """One cell from a financial statement at a specific period end."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    period_end: date
    frequency: Literal["Q", "A"]
    statement: Literal["income", "balance", "cashflow"]
    line_item: str
    value: float | None


# ---- time-series metadata --------------------------------------------------


class DividendRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    ex_date: date
    amount: float = Field(ge=0.0)
    currency: str | None = None
    pay_date: date | None = None
    record_date: date | None = None
    declaration_date: date | None = None


class InsiderTransactionRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    date: date
    owner_name: str | None = None
    owner_relation: str | None = None       # Officer / Director / 10% owner / etc.
    transaction_code: str | None = None     # Sale / Purchase / Option Exercise / etc.
    shares: float | None = None
    price: float | None = None
    value: float | None = None
    vendor_id: str | None = None            # vendor-supplied id for dedup


class NewsArticleRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    published_at: datetime
    title: str
    url: str | None = None
    source_name: str | None = None
    sentiment: float | None = None          # per-article score when provided


class NewsSentimentRow(BaseModel):
    """Daily aggregate sentiment for a ticker (typical vendor shape)."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    date: date
    sentiment: float | None = None
    article_count: int | None = None


class AnalystEstimateRow(BaseModel):
    """One analyst metric at a given period_end (e.g. epsActual, epsEstimate)."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    period_end: date
    metric: str
    value: float | None = None


class AnalystRatingsRow(BaseModel):
    """Current consensus ratings snapshot (one row per ticker)."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    rating: float | None = None             # vendor-defined numeric scale
    target_price: float | None = None
    strong_buy: int = 0
    buy: int = 0
    hold: int = 0
    sell: int = 0
    strong_sell: int = 0
    updated_at: datetime | None = None


class SharesOutstandingRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    date: date
    shares: float = Field(ge=0.0)


class EmployeeCountRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    date: date
    count: int = Field(ge=0)


class SegmentationRow(BaseModel):
    """Revenue or geographic segmentation line for a period.

    One row per (ticker, period_end, dimension, segment).
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    period_end: date
    dimension: Literal["revenue", "geographic"]
    segment: str
    value: float | None = None


# ---- static profile --------------------------------------------------------


class TickerProfile(BaseModel):
    """Static-ish metadata about a ticker. Refreshed periodically but not a
    time-series."""

    model_config = ConfigDict(frozen=True)

    id: str                                   # canonical ticker (e.g. AAPL.US)
    exchange: str | None = None
    currency: str | None = None
    name: str | None = None
    country_iso: str | None = None
    ipo_date: date | None = None
    sector: str | None = None
    industry: str | None = None
    fiscal_year_end: str | None = None
    web_url: str | None = None
    is_delisted: bool = False
    is_bank: bool = False
    beta: float | None = None
    short_percent: float | None = None
    insider_ownership_percent: float | None = None
    institutional_ownership_percent: float | None = None
    employee_count: int | None = None
    esg_score: float | None = None
