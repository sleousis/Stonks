"""MetadataBundle — everything a DataSource can return about a single ticker
that isn't prices or the three financial statements.

The bundle shape lets each ``DataSource`` decide internally whether it hits
one API call (EODHD fundamentals returns most of this at once) or several
(dividends + news + insider are separate EODHD endpoints). The pipeline
just gets a bundle and upserts each non-empty part to the matching lake
table.
"""

from __future__ import annotations

from dataclasses import dataclass

from stonks.ingest.schemas import (
    AnalystEstimateRow,
    AnalystRatingsRow,
    DividendRow,
    EmployeeCountRow,
    InsiderTransactionRow,
    NewsArticleRow,
    NewsSentimentRow,
    SegmentationRow,
    SharesOutstandingRow,
    TickerProfile,
)


@dataclass(frozen=True)
class MetadataBundle:
    profile: TickerProfile | None = None
    dividends: tuple[DividendRow, ...] = ()
    insider_transactions: tuple[InsiderTransactionRow, ...] = ()
    news: tuple[NewsArticleRow, ...] = ()
    news_sentiment: tuple[NewsSentimentRow, ...] = ()
    analyst_estimates: tuple[AnalystEstimateRow, ...] = ()
    analyst_ratings: AnalystRatingsRow | None = None
    shares_outstanding: tuple[SharesOutstandingRow, ...] = ()
    employee_count: tuple[EmployeeCountRow, ...] = ()
    segmentation: tuple[SegmentationRow, ...] = ()
