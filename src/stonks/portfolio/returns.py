"""Daily return history for the covariance constructors (roadmap 9.5.1).

``hrp``, ``erc`` and ``mean_variance_costs`` size a book from a covariance
of past returns. The tick and the backtest build that history here, once
per decision, and hand it to the pipeline on the ``MarketView``:

- :func:`returns_lookback`: how many daily rows the book's constructor reads
  (its ``lookback`` setting), or ``None`` when it reads none (then nothing
  is loaded and the book behaves as before).
- :func:`market_history`: daily returns of adjusted closes and the decision
  bar's volume per ticker, read through a point-in-time view of the lake
  (P12). The view clamps every bar to the decision, so no row after
  ``as_of`` is ever read, even by mistake.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.portfolio.settings import ConstructionSettings

__all__ = ["MarketHistory", "market_history", "returns_lookback"]


@dataclass(frozen=True)
class MarketHistory:
    """``returns``: daily simple returns, one column per ticker (sorted), at
    most ``lookback`` rows ending at the decision; ``None`` when no ticker has
    two bars. ``volumes``: the last visible daily volume per ticker."""

    returns: pd.DataFrame | None = None
    volumes: dict[str, float] = field(default_factory=dict)


def returns_lookback(construction: ConstructionSettings) -> int | None:
    """Daily return rows the book's constructor reads (``None``: none)."""
    if construction.is_single_winner:
        return None
    return construction.build().returns_lookback()


def market_history(view: Any, tickers: Iterable[str], *, lookback: int) -> MarketHistory:
    """Returns and volumes of ``tickers`` as known at ``view``'s decision.

    ``view`` is a :class:`~stonks.store.pit.PointInTimeLake`: its daily bars
    stop at the last one complete at the decision. Closes are the adjusted
    ones (``adj_close``, raw ``close`` where it is missing), so a split or a
    dividend is not read as a return."""
    columns: dict[str, pd.Series] = {}
    volumes: dict[str, float] = {}
    for ticker in sorted(set(tickers)):
        bars = view.get_bars(ticker, Interval.DAY_1, None, None)
        if bars is None or bars.empty:
            continue
        bars = bars.tail(lookback + 1)
        volume = bars["volume"].iloc[-1]
        if volume is not None and not pd.isna(volume) and math.isfinite(float(volume)):
            volumes[ticker] = float(volume)
        close = bars["close"].astype(float)
        adj = bars["adj_close"].astype(float)
        closes = adj.where(adj.notna() & (adj > 0), close).to_numpy()
        index = pd.DatetimeIndex(pd.to_datetime(bars["timestamp"])).normalize()
        series = pd.Series(closes, index=index).pct_change().iloc[1:]
        series = series[np.isfinite(series.to_numpy())]
        if not series.empty:
            columns[ticker] = series
    if not columns:
        return MarketHistory(volumes=volumes)
    frame = pd.DataFrame(columns).sort_index().tail(lookback)
    return MarketHistory(returns=frame, volumes=volumes)
