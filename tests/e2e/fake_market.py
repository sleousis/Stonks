"""Canned market data for the seeded end-to-end stack and the CLI golden run.

Five instruments, all generated from a fixed seed so every run sees the same
numbers:

- ``AAA.US`` rises steadily.
- ``BBB.US`` has a 2:1 split halfway through (raw closes halve, ``adj_close``
  stays continuous).
- ``CCC.US`` pays a 1.00 cash dividend.
- ``SPY.US`` is the benchmark.
- ``BTC-USD.CC`` is crypto and trades every calendar day.

:class:`CannedDataSource` serves them through the ``DataSource`` seam, so
ingest, universes and on-demand fetches all read the same bars without any
network.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from stonks.ingest.metadata_bundle import MetadataBundle
from stonks.ingest.schemas import (
    CryptoProfileRow,
    DividendRow,
    FinancialStatementsBundle,
    RawPriceBar,
    StockSplitRow,
    TickerProfile,
)
from stonks.ingest.sources.base import DataSource

EQUITIES = ("AAA.US", "BBB.US", "CCC.US")
BENCHMARK = "SPY.US"
CRYPTO = "BTC-USD.CC"
TICKERS = (*EQUITIES, BENCHMARK, CRYPTO)

SOURCE_ID = "fake"


@dataclass(frozen=True)
class CannedMarket:
    """Bars, splits, dividends and profiles for a window ending at ``end``."""

    bars: dict[str, list[RawPriceBar]]
    splits: dict[str, list[StockSplitRow]]
    dividends: dict[str, list[DividendRow]]
    profiles: dict[str, TickerProfile]
    crypto: dict[str, CryptoProfileRow]
    start: date
    end: date
    split_date: date
    dividend_date: date


def last_business_day(before: date) -> date:
    """The last weekday strictly before ``before``."""
    day = before - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _path(rng: np.random.Generator, n: int, start: float, drift: float, vol: float) -> np.ndarray:
    steps = rng.normal(drift, vol, size=n)
    steps[0] = 0.0
    return start * np.exp(np.cumsum(steps))


def _bar(ticker: str, day: date, close: float, adj: float, volume: int) -> RawPriceBar:
    close = round(float(close), 4)
    return RawPriceBar(
        ticker=ticker,
        date=day,
        open=round(close * 0.998, 4),
        high=round(close * 1.01, 4),
        low=round(close * 0.99, 4),
        close=close,
        adj_close=round(float(adj), 4),
        volume=volume,
    )


def build_market(end: date, business_days: int = 420, seed: int = 18_4) -> CannedMarket:
    """Deterministic data for ``business_days`` weekdays ending at ``end``."""
    rng = np.random.default_rng(seed)
    days = [d.date() for d in pd.bdate_range(end=end, periods=business_days)]
    n = len(days)
    split_i, div_i = n // 2, (2 * n) // 3
    split_date, dividend_date = days[split_i], days[div_i]

    bars: dict[str, list[RawPriceBar]] = {}
    aaa = _path(rng, n, 50.0, 0.0012, 0.012)
    bars["AAA.US"] = [_bar("AAA.US", d, c, c, 2_000_000) for d, c in zip(days, aaa, strict=True)]

    # BBB: the economic price path, then raw closes halve from the split day.
    bbb = _path(rng, n, 80.0, 0.0006, 0.015)
    bars["BBB.US"] = [
        _bar("BBB.US", d, c if i < split_i else c / 2.0, c / 2.0, 1_500_000)
        for i, (d, c) in enumerate(zip(days, bbb, strict=True))
    ]

    # CCC: a 1.00 dividend; adj_close scales earlier closes by (1 - div/close).
    ccc = _path(rng, n, 40.0, 0.0004, 0.010)
    factor = 1.0 - 1.0 / ccc[div_i - 1]
    bars["CCC.US"] = [
        _bar("CCC.US", d, c, c * factor if i < div_i else c, 1_000_000)
        for i, (d, c) in enumerate(zip(days, ccc, strict=True))
    ]

    spy = _path(rng, n, 400.0, 0.0004, 0.008)
    bars[BENCHMARK] = [_bar(BENCHMARK, d, c, c, 50_000_000) for d, c in zip(days, spy, strict=True)]

    cal = [days[0] + timedelta(days=i) for i in range((end - days[0]).days + 1)]
    btc = _path(rng, len(cal), 30_000.0, 0.001, 0.025)
    bars[CRYPTO] = [_bar(CRYPTO, d, c, c, 10_000) for d, c in zip(cal, btc, strict=True)]

    profiles = {
        "AAA.US": TickerProfile(id="AAA.US", name="Alpha Arcs", exchange="US", currency="USD"),
        "BBB.US": TickerProfile(id="BBB.US", name="Beta Bridges", exchange="US", currency="USD"),
        "CCC.US": TickerProfile(id="CCC.US", name="Gamma Coffee", exchange="US", currency="USD"),
        BENCHMARK: TickerProfile(
            id=BENCHMARK, name="Broad Market ETF", exchange="US", currency="USD"
        ),
        CRYPTO: TickerProfile(
            id=CRYPTO, name="Bitcoin", asset_class="crypto", exchange="CC", currency="USD"
        ),
    }
    return CannedMarket(
        bars=bars,
        splits={"BBB.US": [StockSplitRow(ticker="BBB.US", date=split_date, ratio=2.0)]},
        dividends={
            "CCC.US": [
                DividendRow(ticker="CCC.US", ex_date=dividend_date, amount=1.0, currency="USD")
            ]
        },
        profiles=profiles,
        crypto={
            CRYPTO: CryptoProfileRow(
                ticker=CRYPTO, base_symbol="BTC", quote_symbol="USD", blockchain="bitcoin"
            )
        },
        start=days[0],
        end=end,
        split_date=split_date,
        dividend_date=dividend_date,
    )


class CannedDataSource(DataSource):
    """A ``DataSource`` over a :class:`CannedMarket`. No network."""

    source_id = SOURCE_ID

    def __init__(self, market: CannedMarket) -> None:
        self._market = market

    def list_tickers(self, exchange: str) -> list[str]:
        return sorted(t for t, p in self._market.profiles.items() if p.exchange == exchange)

    def fetch_prices(
        self, ticker: str, since: date | None = None, until: date | None = None
    ) -> Iterable[RawPriceBar]:
        return [
            b
            for b in self._market.bars.get(ticker, [])
            if (since is None or b.date >= since) and (until is None or b.date <= until)
        ]

    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
        return FinancialStatementsBundle()

    def fetch_metadata(self, ticker: str) -> MetadataBundle:
        return MetadataBundle(
            profile=self._market.profiles.get(ticker),
            dividends=tuple(self._market.dividends.get(ticker, ())),
            splits=tuple(self._market.splits.get(ticker, ())),
            crypto_profile=self._market.crypto.get(ticker),
        )
