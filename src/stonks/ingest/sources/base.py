"""Abstract base class for vendor data sources.

A DataSource is a thin HTTP client: it fetches raw rows for a single ticker and
maps them into canonical row schemas (:class:`RawPriceBar`, :class:`FundamentalRow`).
Normalization and persistence are the pipeline's job; sources don't touch the lake.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import date

from stonks.ingest.schemas import FundamentalRow, RawPriceBar


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
