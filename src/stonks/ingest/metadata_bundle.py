"""MetadataBundle — everything a DataSource can return about a single ticker
that isn't prices or the three financial statements.

The bundle shape lets each ``DataSource`` decide internally whether it hits
one API call (EODHD fundamentals returns most of this at once) or several
(dividends + news + insider are separate EODHD endpoints). The pipeline
just gets a bundle and upserts each non-empty part to the matching lake
table.

``analyst_ratings`` is a tuple even though most vendors emit a single
current-snapshot row — keeping it tuple-shaped lets time-series adapters
populate multiple rows in one bundle without a second code path.
"""

from __future__ import annotations

from dataclasses import dataclass

from stonks.ingest.schemas import (
    AnalystForecastRow,
    AnalystRatingsRow,
    BondProfileRow,
    BondYieldRow,
    CommodityContractRow,
    CrossListingRow,
    CryptoProfileRow,
    DividendRow,
    EarningsAnnouncementRow,
    EmployeeCountRow,
    EsgActivityRow,
    EsgSnapshotRow,
    InsiderTransactionRow,
    InstitutionalHolderRow,
    MarketCapRow,
    NewsArticleRow,
    NewsSentimentRow,
    OfficerRow,
    SegmentationRow,
    SharesOutstandingRow,
    StockSplitRow,
    TickerProfile,
    TickerSnapshotRow,
)


@dataclass(frozen=True)
class MetadataBundle:
    profile: TickerProfile | None = None
    ticker_snapshot: TickerSnapshotRow | None = None
    dividends: tuple[DividendRow, ...] = ()
    splits: tuple[StockSplitRow, ...] = ()
    insider_transactions: tuple[InsiderTransactionRow, ...] = ()
    news: tuple[NewsArticleRow, ...] = ()
    news_sentiment: tuple[NewsSentimentRow, ...] = ()
    earnings_announcements: tuple[EarningsAnnouncementRow, ...] = ()
    analyst_forecasts: tuple[AnalystForecastRow, ...] = ()
    analyst_ratings: tuple[AnalystRatingsRow, ...] = ()
    institutional_holders: tuple[InstitutionalHolderRow, ...] = ()
    esg_snapshot: EsgSnapshotRow | None = None
    esg_activities: tuple[EsgActivityRow, ...] = ()
    cross_listings: tuple[CrossListingRow, ...] = ()
    officers: tuple[OfficerRow, ...] = ()
    shares_outstanding: tuple[SharesOutstandingRow, ...] = ()
    employee_count: tuple[EmployeeCountRow, ...] = ()
    segmentation: tuple[SegmentationRow, ...] = ()
    market_cap_history: tuple[MarketCapRow, ...] = ()
    # Per-asset-class profiles. Equity sources leave these at the defaults;
    # crypto/bond/commodity sources populate the matching field.
    crypto_profile: CryptoProfileRow | None = None
    bond_profile: BondProfileRow | None = None
    commodity_contract: CommodityContractRow | None = None
    bond_yields: tuple[BondYieldRow, ...] = ()
