"""Canonical ingestion row schemas.

These are the *normalized*, vendor-agnostic rows that flow from any
``DataSource`` into the lake — vendor-specific shapes are translated up-front,
so the pipeline and the lake never see vendor fields.

Schemas cover the full fundamentals surface area (dividends, insider trades,
news, analyst data, shares outstanding, employee count, segmentations,
profile, holders, earnings events, ESG, listings, officers) — not just the
three financial statements — so strategies can draw on whatever they need
without each layer having to be extended.

Vendor-agnostic principle (CLAUDE.md): fields whose vocabulary varies across
vendors use ``Literal`` values that adapters map their raw strings into;
fields with no cross-vendor analogue are not modelled here.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Re-export for DataSource implementations that need to import from the
# schemas module in a single statement.
__all__ = [
    "AnalystForecastRow",
    "AnalystRatingsRow",
    "CrossListingRow",
    "DividendRow",
    "EarningsAnnouncementRow",
    "EmployeeCountRow",
    "EsgActivityRow",
    "EsgSnapshotRow",
    "FundamentalRow",
    "InsiderTransactionRow",
    "InstitutionalHolderRow",
    "IntradayBar",
    "MarketCapRow",
    "NewsArticleRow",
    "NewsSentimentRow",
    "OfficerRow",
    "RawPriceBar",
    "SegmentationRow",
    "SharesOutstandingRow",
    "StockSplitRow",
    "TickerProfile",
    "TickerSnapshotRow",
]

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


class IntradayBar(BaseModel):
    """Sub-daily bar — same shape as :class:`RawPriceBar` but with a full
    ``datetime`` instead of a plain date. Flows directly into the ``bars``
    table via ``DuckDBLake.upsert_bars`` with an explicit ``interval``."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    timestamp: datetime
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
    """One insider trade. Captures both the trade event and the SEC filing.

    ``transaction_date`` is the actual trade; ``filing_date`` is when the
    Form 4 was filed (typically a few days later). Both are useful: the lag
    itself is a signal, and event-study windows anchor on the trade date
    while disclosure-flow studies anchor on the filing date.
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    transaction_date: date
    filing_date: date | None = None
    owner_name: str | None = None
    owner_cik: str | None = None
    owner_relation: str | None = None  # categorical: officer / director / 10% owner
    owner_title: str | None = None  # free text e.g. "CEO"
    transaction_code: str | None = None  # SEC Form 4 code (S, P, A, M, …)
    acquired_disposed: Literal["A", "D"] | None = None
    shares: float | None = None
    price: float | None = None
    value: float | None = None  # derived: shares * price
    post_transaction_amount: float | None = None
    sec_link: str | None = None  # URL to the underlying Form 4


class NewsArticleRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    published_at: datetime
    title: str
    url: str | None = None
    source_name: str | None = None
    sentiment: float | None = None  # per-article score when provided


class NewsSentimentRow(BaseModel):
    """Daily aggregate sentiment for a ticker (typical vendor shape)."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    date: date
    sentiment: float | None = None
    article_count: int | None = None


class EarningsAnnouncementRow(BaseModel):
    """One earnings event for a fiscal period.

    Holds both the actual reported result and the analyst consensus going in,
    so the row directly answers "did they beat?" via ``surprise_percent``.
    Future periods (not yet announced) have ``eps_actual`` = None but still
    carry ``eps_estimate`` and ``report_date``.

    ``before_after_market`` is normalized: vendors expose strings like
    EODHD's ``BeforeMarket`` / ``AfterMarket`` / ``DuringMarket``; adapters
    map them into ``before`` / ``after`` / ``during``.
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    period_end: date
    report_date: date | None = None
    before_after_market: Literal["before", "during", "after"] | None = None
    currency: str | None = None
    eps_actual: float | None = None
    eps_estimate: float | None = None
    eps_difference: float | None = None
    surprise_percent: float | None = None


class AnalystForecastRow(BaseModel):
    """Analyst dispersion, revenue forecasts, and EPS revision history for a
    given fiscal period.

    A single ``period_end`` can carry multiple rows when the vendor reports
    several relative offsets (e.g. the same quarter labelled both as
    ``current_quarter`` and ``current_year`` aggregates). ``period_relative``
    is the discriminator: vendors emit strings like EODHD's ``0q`` /
    ``+1q`` / ``+1y``; adapters map them into the literal below.
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    period_end: date
    period_relative: (
        Literal[
            "prior_year",
            "prior_quarter",
            "current_quarter",
            "next_quarter",
            "current_year",
            "next_year",
        ]
        | None
    ) = None
    growth: float | None = None

    # EPS forecast dispersion
    eps_estimate_avg: float | None = None
    eps_estimate_low: float | None = None
    eps_estimate_high: float | None = None
    eps_estimate_year_ago: float | None = None
    eps_estimate_n_analysts: int | None = None
    eps_estimate_growth: float | None = None

    # Revenue forecast dispersion
    revenue_estimate_avg: float | None = None
    revenue_estimate_low: float | None = None
    revenue_estimate_high: float | None = None
    revenue_estimate_year_ago: float | None = None
    revenue_estimate_n_analysts: int | None = None
    revenue_estimate_growth: float | None = None

    # EPS estimate revision history
    eps_trend_current: float | None = None
    eps_trend_7d_ago: float | None = None
    eps_trend_30d_ago: float | None = None
    eps_trend_60d_ago: float | None = None
    eps_trend_90d_ago: float | None = None

    # Revision activity counts
    eps_revisions_up_7d: int | None = None
    eps_revisions_up_30d: int | None = None
    eps_revisions_down_7d: int | None = None
    eps_revisions_down_30d: int | None = None


class AnalystRatingsRow(BaseModel):
    """Analyst consensus ratings + target price for a ticker, with an
    explicit ``snapshot_date`` so the row is a time series — not a
    single-row-per-ticker snapshot.

    Ingested via change-detection (``_upsert_on_change``) so the table only
    grows when the consensus actually moves.
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    snapshot_date: date
    rating: float | None = None  # vendor-defined numeric scale
    target_price: float | None = None
    strong_buy: int = 0
    buy: int = 0
    hold: int = 0
    sell: int = 0
    strong_sell: int = 0


class InstitutionalHolderRow(BaseModel):
    """One institutional / fund holder snapshot for a ticker.

    EODHD returns top-20 institutions and top-20 funds; the same row schema
    serves both, discriminated by ``holder_kind``. Ingested via
    change-detection: a new row is only inserted when one of the observed
    values (shares, assets %, change) actually moves.
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    holder_kind: Literal["institution", "fund"]
    name: str
    snapshot_date: date
    total_shares_pct: float | None = None  # % of issuer's outstanding shares
    total_assets_pct: float | None = None  # % of holder's portfolio
    current_shares: int | None = None
    change_shares: int | None = None
    change_pct: float | None = None


class EsgSnapshotRow(BaseModel):
    """ESG ratings header. Time-series via ``rating_date`` + change-detection."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    rating_date: date
    total_esg: float | None = None
    total_esg_percentile: float | None = None
    environment_score: float | None = None
    environment_percentile: float | None = None
    social_score: float | None = None
    social_percentile: float | None = None
    governance_score: float | None = None
    governance_percentile: float | None = None
    controversy_level: int | None = None


class EsgActivityRow(BaseModel):
    """One controversial-activity flag for an ESG snapshot
    (e.g. ``activity='alcohol', involvement='No'``).

    Vendors use slightly different activity vocabularies; we keep both
    fields free-form so any vendor can populate without being locked to
    one taxonomy.
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    rating_date: date
    activity: str
    involvement: str  # typically "Yes" / "No"


class CrossListingRow(BaseModel):
    """A secondary listing of the same security on another exchange.

    The row's ``ticker`` is the *canonical* (primary) ticker; ``exchange`` +
    ``exchange_code`` identify where else it trades.
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    exchange: str
    exchange_code: str
    name: str | None = None


class OfficerRow(BaseModel):
    """One executive on the current officer roster of an issuer.

    EODHD returns a current-state list with no per-officer date; we model
    this as a current-snapshot table (``upsert_officers`` deletes the
    ticker's existing officers before inserting), not a time series.
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    name: str
    title: str | None = None
    year_born: int | None = None


class TickerSnapshotRow(BaseModel):
    """Volatile metric snapshot for a ticker — extracted from
    ``TickerProfile`` so the static profile stays static and these
    drift-prone metrics get a proper time series.

    Ingested via change-detection.
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    snapshot_date: date
    beta: float | None = None
    short_percent: float | None = None
    percent_insiders: float | None = None
    percent_institutions: float | None = None


class SharesOutstandingRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    date: date
    shares: float = Field(ge=0.0)


class StockSplitRow(BaseModel):
    """One stock split event. ``ratio > 1`` is a forward split (2:1 → 2.0);
    ``ratio < 1`` is a reverse split (1:10 → 0.1)."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    date: date
    ratio: float = Field(gt=0.0)


class MarketCapRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    date: date
    market_cap: float = Field(ge=0.0)


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
    time series — volatile metrics like ownership percentages, beta and
    short interest live on :class:`TickerSnapshotRow` instead.

    ``security_type`` is the vendor-agnostic kind (``common_stock``,
    ``preferred_stock``, ``adr``, ``etf``, ``fund``, ``other``); adapters
    map their vendor strings (e.g. EODHD's ``Common Stock`` / ``ETF``)
    into this literal.
    """

    model_config = ConfigDict(frozen=True)

    id: str  # canonical ticker (e.g. AAPL.US)
    exchange: str | None = None
    currency: str | None = None
    name: str | None = None

    # Country + classification
    country_iso: str | None = None
    sector: str | None = None
    industry: str | None = None
    gic_sector: str | None = None
    gic_group: str | None = None
    gic_industry: str | None = None
    gic_sub_industry: str | None = None

    # Lifecycle
    ipo_date: date | None = None
    is_delisted: bool = False
    delisted_date: date | None = None
    is_bank: bool = False
    fiscal_year_end: str | None = None
    security_type: (
        Literal["common_stock", "preferred_stock", "adr", "etf", "fund", "other"] | None
    ) = None

    # Identifiers
    cusip: str | None = None
    cik: str | None = None
    isin: str | None = None
    open_figi: str | None = None
    lei: str | None = None
    employer_id_number: str | None = None
    primary_ticker: str | None = None

    # Address + contact
    address_street: str | None = None
    address_city: str | None = None
    address_state: str | None = None
    address_country: str | None = None
    address_zip: str | None = None
    phone: str | None = None
    web_url: str | None = None

    # Misc
    description: str | None = None
    updated_at: datetime | None = None
