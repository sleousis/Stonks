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

from dataclasses import dataclass, field
from typing import Literal

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
    FinancialStatementsBundle,
    FundHoldingRow,
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

    def __post_init__(self) -> None:
        # A single ticker can only belong to one asset class, so at most
        # one of the per-class profile fields may be set per bundle.
        # Catches the "adapter copy-pasted between branches" bug.
        per_class = [
            self.crypto_profile,
            self.bond_profile,
            self.commodity_contract,
        ]
        non_null = sum(1 for x in per_class if x is not None)
        if non_null > 1:
            raise ValueError(
                "MetadataBundle: at most one of "
                "{crypto_profile, bond_profile, commodity_contract} may be set per bundle"
            )


#: The parts of one company's data a caller can ask for at once
#: (:meth:`~stonks.ingest.sources.base.DataSource.fetch_company_data`).
#: ``profile`` is the profile and everything that rides with it (snapshot,
#: analysts, holders, officers, shares outstanding, ESG, earnings history).
CompanyPart = Literal[
    "profile",
    "statements",
    "fund_holdings",
    "dividends",
    "splits",
    "market_cap",
    "news",
    "sentiment",
    "insiders",
]
COMPANY_PARTS: tuple[CompanyPart, ...] = (
    "profile",
    "statements",
    "fund_holdings",
    "dividends",
    "splits",
    "market_cap",
    "news",
    "sentiment",
    "insiders",
)
#: The parts :class:`MetadataBundle` carries.
METADATA_PARTS: frozenset[CompanyPart] = frozenset(
    {"profile", "dividends", "splits", "market_cap", "news", "sentiment", "insiders"}
)


@dataclass(frozen=True)
class CompanyData:
    """The parts of one company's data a caller asked for. ``statements``
    is ``None`` when they were not asked for, so an upsert leaves the
    stored statements alone."""

    metadata: MetadataBundle = field(default_factory=MetadataBundle)
    statements: FinancialStatementsBundle | None = None
    fund_holdings: tuple[FundHoldingRow, ...] = ()
