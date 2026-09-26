"""Shared helpers for strategy examples.

Re-exports the cross-layer timeutil helpers under a strategy-local namespace
so strategy examples import from a single obvious module rather than
reaching into ``core`` directly, and hosts :func:`get_last_n_bars`, the
bar-count history fetch every lookback-based strategy uses.
"""

from __future__ import annotations

import math
import weakref
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.timeutil import as_datetime, iso

__all__ = ["BarCache", "LakeBarCaches", "as_datetime", "get_last_n_bars", "iso"]

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


# ---- shared bar cache -------------------------------------------------------

# Bounds wide enough to cover every bar a lake can hold.
_HISTORY_START = datetime(1900, 1, 1)
_HISTORY_END = datetime(2200, 1, 1)


class _Series:
    """One ticker's full bar history at one interval, oldest first."""

    __slots__ = ("closes", "frame", "timestamps")

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame.reset_index(drop=True)
        if frame.empty:
            self.timestamps = np.array([], dtype="datetime64[us]")
            self.closes = np.array([], dtype=float)
        else:
            self.timestamps = pd.to_datetime(frame["timestamp"]).to_numpy(dtype="datetime64[us]")
            self.closes = frame["close"].to_numpy(dtype=float)

    def end_index(self, as_of: Any) -> int:
        """Number of bars with ``timestamp <= as_of``."""
        cutoff = np.datetime64(as_datetime(as_of), "us")
        return int(np.searchsorted(self.timestamps, cutoff, side="right"))


class BarCache:
    """Read-through, look-ahead-safe bar cache over one lake.

    The first read of a ``(ticker, interval)`` pulls that series' whole
    history in one query; every later read slices it in memory. A backtest
    asks for the trailing window on every bar, so this replaces one query
    per ticker per bar with one query per ticker per run.

    Look-ahead safety: the cache holds bars after ``as_of`` (it holds the
    whole series), so every accessor slices strictly on
    ``timestamp <= as_of`` (or ``<= end``) before returning anything, and
    returns copies so callers can't mutate the cached history.

    Rows written to the lake after a series is first read are not seen.
    Scope a cache to something short-lived (one strategy instance, which is
    one backtest trial or one production tick).
    """

    def __init__(self, lake: Any, *, weak: bool = False) -> None:
        # ``weak=True`` (used by :class:`LakeBarCaches`) keeps the cache from
        # pinning its lake alive when it is stored under that lake as a key.
        self._lake: Any = weakref.ref(lake) if weak else (lambda: lake)
        self._series: dict[tuple[str, str], _Series] = {}

    def _get(self, ticker: str, interval: Interval) -> _Series:
        key = (ticker, interval.code)
        series = self._series.get(key)
        if series is None:
            df = self._lake().get_bars(ticker, interval, start=_HISTORY_START, end=_HISTORY_END)
            series = _Series(df if df is not None else pd.DataFrame())
            self._series[key] = series
        return series

    def last_n_bars(self, ticker: str, interval: Interval, as_of: Any, n: int) -> pd.DataFrame:
        """Same contract as :func:`get_last_n_bars`: the last ``n`` bars with
        ``timestamp <= as_of``, oldest first, re-indexed ``0..len-1``."""
        series = self._get(ticker, interval)
        if n <= 0 or series.frame.empty:
            return series.frame.iloc[0:0].copy()
        end = series.end_index(as_of)
        return series.frame.iloc[max(0, end - n) : end].reset_index(drop=True)

    def last_n_closes(self, ticker: str, interval: Interval, as_of: Any, n: int) -> np.ndarray:
        """Closes of :meth:`last_n_bars` as a float array (a copy)."""
        series = self._get(ticker, interval)
        if n <= 0:
            return np.array([], dtype=float)
        end = series.end_index(as_of)
        return series.closes[max(0, end - n) : end].copy()

    def bars_between(self, ticker: str, interval: Interval, start: Any, end: Any) -> pd.DataFrame:
        """Bars with ``start <= timestamp <= end``, oldest first, like
        ``lake.get_bars``."""
        series = self._get(ticker, interval)
        if series.frame.empty:
            return series.frame.iloc[0:0].copy()
        lo = int(
            np.searchsorted(series.timestamps, np.datetime64(as_datetime(start), "us"), "left")
        )
        hi = series.end_index(end)
        return series.frame.iloc[lo:hi].reset_index(drop=True)


class LakeBarCaches:
    """One :class:`BarCache` per lake, held weakly.

    Strategies keep one of these per instance: the lab's permutation and
    perturbation tests hand the same strategy different lakes, which must
    never share bars. A lake that can't be weakly referenced gets a fresh
    (uncached, but correct) :class:`BarCache` on every call.
    """

    def __init__(self) -> None:
        self._caches: weakref.WeakKeyDictionary[Any, BarCache] = weakref.WeakKeyDictionary()

    def for_lake(self, lake: Any) -> BarCache:
        try:
            cache = self._caches.get(lake)
            if cache is None:
                cache = BarCache(lake, weak=True)
                self._caches[lake] = cache
        except TypeError:
            return BarCache(lake)
        return cache

    def __len__(self) -> int:
        return len(self._caches)
