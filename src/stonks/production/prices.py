"""Prices the production tick marks, sells and buys with.

One rule, shared by the real tick and shadow mode: a close older than the
staleness window may still *mark* a holding and *sell* it (the last known
close is the best estimate there is), but it must never fund a *new buy*
(a delisted or failed-ingest ticker would otherwise be bought at a
months-old price).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from stonks.core.timeutil import day_start
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
    #: Date of the bar each price comes from.
    bar_dates: dict[str, date] = field(default_factory=dict)


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
    bar_dates: dict[str, date] = {}
    for record in df.to_dict("records"):
        ticker = str(record["ticker"])
        bar_date: date = pd.Timestamp(record["date"]).date()  # type: ignore[assignment]
        is_fresh = bar_date >= oldest
        if is_fresh:
            fresh.add(ticker)
        if is_fresh or ticker in held_set:
            prices[ticker] = float(record["close"])
            bar_dates[ticker] = bar_date
            if not pd.isna(record["volume"]):
                volumes[ticker] = float(record["volume"])
    return PriceBook(prices=prices, fresh=frozenset(fresh), volumes=volumes, bar_dates=bar_dates)


_HISTORY_COLUMNS = ["open", "high", "low", "close", "volume"]


def load_history(
    lake: DuckDBLake, tickers: Sequence[str], as_of: date, *, bars: int = 260
) -> dict[str, pd.DataFrame]:
    """The last ``bars`` daily bars at or before ``as_of`` per ticker, in one
    query: a date-indexed frame (oldest first) of adjusted
    ``open, high, low, close, volume``. OHLC are scaled by
    ``adj_close / close`` (raw where either is missing). Tickers without
    bars are absent."""
    if not tickers or bars < 1:
        return {}
    df = lake.sql(
        """
        SELECT ticker, CAST(timestamp AS DATE) AS date,
               open, high, low, close, adj_close, volume
          FROM (
            SELECT *, row_number() OVER (PARTITION BY ticker ORDER BY timestamp DESC) AS rn
              FROM bars
             WHERE interval = '1d' AND ticker = ANY(?) AND timestamp < ?
          )
         WHERE rn <= ?
         ORDER BY ticker, timestamp
        """,
        [sorted(set(tickers)), day_start(as_of + timedelta(days=1)), bars],
    )
    if df.empty:
        return {}
    factor = (df["adj_close"] / df["close"]).where(df["close"] > 0).fillna(1.0)
    for col in ("open", "high", "low", "close"):
        df[col] = df[col] * factor
    df["date"] = pd.to_datetime(df["date"])
    return {
        str(ticker): group.set_index("date")[_HISTORY_COLUMNS].rename_axis("date")
        for ticker, group in df.groupby("ticker", sort=True)
    }


def drop_stale_opens(
    orders: Sequence[Order],
    fresh: Collection[str],
    positions: Mapping[str, float] | None = None,
) -> tuple[list[Order], list[str]]:
    """Split off opening orders (buys and short sales) of tickers without a
    fresh close; closes (sells of longs, covers of shorts) always pass, so a
    stale holding can still be exited (BE-11). An order's position effect
    decides; without one the held quantity does (a buy opens unless it
    covers a short, a sell opens only from flat or short). Without
    ``positions`` every buy opens and every sell closes.
    Returns (kept orders, tickers whose opening orders were dropped)."""
    kept: list[Order] = []
    dropped: list[str] = []
    for order in orders:
        if order.ticker not in fresh and _opens(order, positions):
            dropped.append(order.ticker)
            continue
        kept.append(order)
    if dropped:
        _log.info("prices.stale_opens_dropped", tickers=dropped)
    return kept, dropped


#: The old name (buys were the only opening orders before shorts).
drop_stale_buys = drop_stale_opens


def _opens(order: Order, positions: Mapping[str, float] | None) -> bool:
    if order.position_effect is not None:
        return order.position_effect == "open"
    if positions is None:
        return order.side == "buy"
    qty = positions.get(order.ticker, 0.0)
    return qty >= 0 if order.side == "buy" else qty <= 1e-12


def held_tickers(positions: dict[str, float]) -> list[str]:
    return [t for t, q in positions.items() if abs(q) > 1e-12]
