"""Prices the production tick marks, sells and buys with.

One rule, shared by the real tick and shadow mode: a close older than the
staleness window may still *mark* a holding and *sell* it (the last known
close is the best estimate there is), but it must never fund a *new buy*
(a delisted or failed-ingest ticker would otherwise be bought at a
months-old price).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from stonks.core.types import Order
from stonks.logging import get_logger
from stonks.store.lake import DuckDBLake

_log = get_logger("stonks.production.prices")


@dataclass(frozen=True)
class PriceBook:
    #: Latest close at or before ``as_of`` per ticker: fresh universe
    #: tickers plus every held ticker, however old its close.
    prices: dict[str, float]
    #: Tickers whose close is inside the staleness window (buyable).
    fresh: frozenset[str]
    #: Volume of the bar each price comes from (the cost model's impact
    #: input); tickers whose bar has no volume are absent.
    volumes: dict[str, float] = field(default_factory=dict)


def load_prices(
    lake: DuckDBLake,
    universe: Sequence[str],
    held: Iterable[str],
    as_of: date,
    *,
    max_staleness_days: int,
) -> PriceBook:
    held_set = set(held)
    tickers = sorted(set(universe) | held_set)
    if not tickers:
        return PriceBook(prices={}, fresh=frozenset())
    oldest = as_of - timedelta(days=max_staleness_days)
    # Latest close per ticker in one grouped query (no N+1 LIMIT-1 queries).
    df = lake.sql(
        """
        SELECT ticker, arg_max(close, date) AS close, arg_max(volume, date) AS volume,
               max(date) AS date
          FROM prices
         WHERE ticker = ANY(?) AND date <= ?
         GROUP BY ticker
        """,
        [tickers, as_of],
    )
    prices: dict[str, float] = {}
    volumes: dict[str, float] = {}
    fresh: set[str] = set()
    for row in df.itertuples(index=False):
        is_fresh = pd.Timestamp(row.date).date() >= oldest
        if is_fresh:
            fresh.add(row.ticker)
        if is_fresh or row.ticker in held_set:
            prices[row.ticker] = float(row.close)
            if not pd.isna(row.volume):
                volumes[row.ticker] = float(row.volume)
    return PriceBook(prices=prices, fresh=frozenset(fresh), volumes=volumes)


def drop_stale_buys(
    orders: Sequence[Order], fresh: Collection[str]
) -> tuple[list[Order], list[str]]:
    """Split off buys of tickers without a fresh close; sells always pass.
    Returns (kept orders, tickers whose buys were dropped)."""
    kept: list[Order] = []
    dropped: list[str] = []
    for order in orders:
        if order.side == "buy" and order.ticker not in fresh:
            dropped.append(order.ticker)
            continue
        kept.append(order)
    if dropped:
        _log.info("prices.stale_buys_dropped", tickers=dropped)
    return kept, dropped


def held_tickers(positions: dict[str, float]) -> list[str]:
    return [t for t, q in positions.items() if abs(q) > 1e-12]
