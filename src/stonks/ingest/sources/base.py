"""Abstract base class for vendor data sources.

A DataSource is a thin HTTP client: it fetches raw rows for a single ticker and
maps them into canonical row schemas (:class:`RawPriceBar`, :class:`FundamentalRow`,
or a :class:`MetadataBundle` covering everything else).

Normalization and persistence are the pipeline's job; sources don't touch the lake.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import date

from stonks.core.interval import Interval
from stonks.ingest.metadata_bundle import MetadataBundle
from stonks.ingest.schemas import FundamentalRow, IntradayBar, RawPriceBar


class DataSource(ABC):
    source_id: str  # unique id, e.g. "eodhd", "yahoo"

    @abstractmethod
    def list_tickers(self, exchange: str) -> list[str]: ...

    @abstractmethod
    def fetch_prices(
        self, ticker: str, since: date | None = None, until: date | None = None
    ) -> Iterable[RawPriceBar]: ...

    @abstractmethod
    def fetch_fundamentals(self, ticker: str) -> Iterable[FundamentalRow]: ...

    # Extended fundamentals: ticker profile, dividends, insiders, news, analysts,
    # shares outstanding, employees, segmentation — all in one bundle. Sources
    # that only cover a subset return an empty bundle + populate only what
    # they have. Default implementation returns an empty bundle so existing
    # subclasses don't have to opt in.
    def fetch_metadata(self, ticker: str) -> MetadataBundle:
        return MetadataBundle()

    def fetch_intraday_bars(
        self,
        ticker: str,
        interval: Interval,
        since: date | None = None,
        until: date | None = None,
    ) -> Iterable[IntradayBar]:
        """Return sub-daily bars at the given native interval. Subclasses
        that don't support intraday data can leave the default empty
        implementation."""
        return ()
