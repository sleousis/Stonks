"""Shared helpers for strategy examples.

Re-exports the cross-layer timeutil helpers under a strategy-local namespace
so strategy examples import from a single obvious module rather than
reaching into ``core`` directly, and hosts :func:`get_last_n_bars`, the
bar-count history fetch every lookback-based strategy uses.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from stonks.core.interval import Interval
from stonks.core.timeutil import as_datetime, iso

__all__ = ["as_datetime", "get_last_n_bars", "iso"]

# Shortest regular session we plan for (US cash equities: 6.5 hours).
# Markets that trade longer (futures, crypto) simply over-fetch a little.
_MIN_SESSION = timedelta(hours=6, minutes=30)
# Calendar slack on top of the weekday estimate for holidays / half days.
_HOLIDAY_SLACK = timedelta(days=5)
# How many times the window is doubled when it still holds too few bars
# (long exchange closures, sparse data). 2**6 = 64x the initial estimate.
_MAX_WIDENINGS = 6


def _initial_span(interval: Interval, n: int) -> timedelta:
    """Calendar span that holds ``n`` bars of ``interval`` on a
    weekday-only market with short sessions and a few holidays."""
    bar = interval.to_timedelta()
    if interval.is_intraday:
        bars_per_session = max(1, math.floor(_MIN_SESSION / bar))
        sessions = math.ceil(n / bars_per_session)
        return timedelta(days=math.ceil(sessions * 7 / 5)) + _HOLIDAY_SLACK
    if bar < timedelta(days=7):
        # daily-ish bars only print on weekdays
        return bar * math.ceil(n * 7 / 5) + _HOLIDAY_SLACK
    return bar * (n + 1)


def _window_start(end: datetime, span: timedelta) -> datetime:
    try:
        return end - span
    except OverflowError:
        return datetime(1, 1, 1)


def get_last_n_bars(lake: Any, ticker: str, interval: Interval, as_of: Any, n: int) -> pd.DataFrame:
    """Return the last ``n`` bars of ``ticker`` at ``interval`` with
    timestamp ``<= as_of``, oldest first, re-indexed ``0..len-1``.

    Lookbacks are defined in bars, but markets close overnight, at
    weekends and on holidays, so a window of ``n * interval`` wall-clock
    time can hold far fewer than ``n`` bars. We start from a calendar
    estimate that allows for weekends, short sessions and holidays, and
    widen it until it holds ``n`` bars. When the lake simply doesn't have
    ``n`` bars of history the result is shorter; callers check ``len``.
    """
    end_dt = as_datetime(as_of)
    span = _initial_span(interval, n)
    df = lake.get_bars(ticker, interval, start=_window_start(end_dt, span), end=as_of)
    for _ in range(_MAX_WIDENINGS):
        if df is None or len(df) >= n:
            break
        try:
            span *= 2
        except OverflowError:
            break
        df = lake.get_bars(ticker, interval, start=_window_start(end_dt, span), end=as_of)
    if df is None:
        return pd.DataFrame()
    return df.iloc[-n:].reset_index(drop=True)
