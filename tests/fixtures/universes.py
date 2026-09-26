"""Shared fakes for the universe and data-ensure tests: a data source with
canned listings and prices (no network) and a daily-bar seeder."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from datetime import date

import pandas as pd

from stonks.ingest.schemas import FinancialStatementsBundle, RawPriceBar, SymbolListing
from stonks.ingest.sources.base import DataSource, DataSourceError, UnsupportedCapabilityError


def seed_daily_bars(
    lake, ticker: str, start: date, end: date, *, close: float = 10.0, volume: int = 1000
) -> None:
    days = pd.bdate_range(start, end)
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": ticker,
                "date": [d.date() for d in days],
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "adj_close": close,
                "volume": volume,
            }
        )
    )


def bars(ticker: str, start: date, end: date, close: float = 10.0) -> list[RawPriceBar]:
    return [
        RawPriceBar(
            ticker=ticker,
            date=d.date(),
            open=close,
            high=close * 1.01,
            low=close * 0.99,
            close=close,
            adj_close=close,
            volume=1000,
        )
        for d in pd.bdate_range(start, end)
    ]


class FakeListingSource(DataSource):
    """Canned listings and prices. ``fetch_prices`` honours since/until and
    records every call; tickers in ``failing`` raise a soft-fail error."""

    source_id = "fake"

    def __init__(
        self,
        listings: Iterable[SymbolListing] = (),
        prices: dict[str, list[RawPriceBar]] | None = None,
        *,
        failing: Iterable[str] = (),
        bulk: bool = False,
    ) -> None:
        self._listings = list(listings)
        self._prices = prices or {}
        self._failing = set(failing)
        self._bulk = bulk
        self._lock = threading.Lock()
        self.calls: list[str] = []
        self.price_calls: list[tuple[str, date | None, date | None]] = []
        self.bulk_calls: list[tuple[str, date]] = []

    def list_tickers(self, exchange: str) -> list[str]:
        return [s.ticker for s in self.list_symbols(exchange)]

    def list_symbols(self, exchange: str) -> list[SymbolListing]:
        self.calls.append(exchange)
        return list(self._listings)

    def fetch_prices(
        self, ticker: str, since: date | None = None, until: date | None = None
    ) -> Iterable[RawPriceBar]:
        with self._lock:
            self.price_calls.append((ticker, since, until))
        if ticker in self._failing:
            raise DataSourceError(f"no data for {ticker}")
        return [
            b
            for b in self._prices.get(ticker, [])
            if (since is None or b.date >= since) and (until is None or b.date <= until)
        ]

    def fetch_bulk_eod(self, exchange: str, day: date) -> list[RawPriceBar]:
        if not self._bulk:
            raise UnsupportedCapabilityError("no bulk")
        with self._lock:
            self.bulk_calls.append((exchange, day))
        return [
            b
            for rows in self._prices.values()
            for b in rows
            if b.date == day and b.ticker.endswith(f".{exchange}")
        ]

    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
        return FinancialStatementsBundle()
