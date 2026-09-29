"""Shared helpers for strategy examples.

Re-exports the cross-layer timeutil helpers under a strategy-local namespace
so strategy examples import from a single obvious module rather than
reaching into ``core`` directly, and hosts :func:`get_last_n_bars`, the
bar-count history fetch every lookback-based strategy uses.

Price basis
-----------
Every bar accessor takes ``basis``: ``"adjusted"`` (the default, for
signals) back-adjusts OHLC and volume for splits and dividends **as of**
the read's cutoff, using only events effective on or before it (see
``stonks.features.price_adjustment``); ``"raw"`` returns what the market
quoted. The latest bar as of the cutoff is identical in both, so adjusted
levels line up with the raw prices orders fill at. A ticker with no
corporate-action data (and ``adj_close`` equal to ``close``) reads exactly
the same in both bases.

Bar visibility (RS-03)
----------------------
The engine calls a strategy with ``as_of`` = the start of the decision bar
and decides at that bar's close. A bar of interval ``I`` stamped ``S`` is
complete at ``S + I``, so a decision on a bar of length ``L`` may see it
when ``S + I <= as_of + L`` (:func:`visible_cutoff`). The "as of" readers of
:class:`BarCache` (``last_n_bars``, ``last_n_closes``, ``last_close``) apply
this rule, so a daily bar stays hidden during its own session in an
intraday run, and a 4h bar stays hidden inside a 1h run until it closes.

``L`` comes from :func:`decision_interval` when the caller sets it (the
backtest engine and the tick know their bar). Without it: an intraday read
keeps every bar stamped on or before ``as_of`` (the bar the decision stands
on), a daily or coarser read at a midnight ``as_of`` is a daily decision
(``L`` = one day, the day's own bar counts), and at any other time of day
only bars already closed at ``as_of`` count. The last case is conservative
on 24/7 markets. The one blind spot is a 24/7 intraday run's midnight bar
with no decision interval set, which reads like a daily decision.
"""

from __future__ import annotations

import copy
import dataclasses
import math
import weakref
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.corporate_actions import CorporateAction, PriceBasis
from stonks.core.interval import Interval
from stonks.core.interval import known_through as core_known_through
from stonks.core.interval import visible_cutoff as core_visible_cutoff
from stonks.core.timeutil import as_datetime, iso
from stonks.core.types import Order, Portfolio
from stonks.features.price_adjustment import SeriesAdjustment
from stonks.store.corporate_actions import LakeCorporateActions
from stonks.store.pit import PointInTimeLake

__all__ = [
    "BarCache",
    "LakeBarCaches",
    "as_datetime",
    "close_all",
    "closes_only",
    "decision_interval",
    "get_last_n_bars",
    "iso",
    "long_only_decide",
    "memo_scope",
    "sell_all_longs",
    "split_effects",
    "visible_cutoff",
]

# Shortest regular session we plan for (US cash equities: 6.5 hours).
# Markets that trade longer (futures, crypto) simply over-fetch a little.
_MIN_SESSION = timedelta(hours=6, minutes=30)
# Calendar slack on top of the weekday estimate for holidays / half days.
_HOLIDAY_SLACK = timedelta(days=5)
# How many times the window is doubled when it still holds too few bars
# (long exchange closures, sparse data). 2**6 = 64x the initial estimate.
_MAX_WIDENINGS = 6


_DECISION_INTERVAL: ContextVar[Interval | None] = ContextVar(
    "stonks_decision_interval", default=None
)
_ONE_DAY = timedelta(days=1)


@contextmanager
def decision_interval(interval: Interval | None) -> Iterator[None]:
    """Declare the bar length of the decisions made inside the block (see
    the module doc). The engine and the tick wrap their strategy calls in
    it; ``None`` falls back to the midnight rule."""
    token = _DECISION_INTERVAL.set(interval)
    try:
        yield
    finally:
        _DECISION_INTERVAL.reset(token)


def visible_cutoff(as_of: Any, interval: Interval) -> datetime:
    """The latest bar stamp of ``interval`` that is complete at a decision
    on the bar starting at ``as_of``: ``as_of + L - I`` (see the module
    doc). Months and years use calendar offsets. The rule itself lives in
    :func:`stonks.core.interval.visible_cutoff`; this reads ``L`` from
    :func:`decision_interval`."""
    return core_visible_cutoff(as_datetime(as_of), interval, _DECISION_INTERVAL.get())


def known_day(as_of: Any) -> date:
    """The last calendar day whose day-stamped rows (a macro print, a
    yield, a filing) are known at a decision on the bar starting at
    ``as_of``: that day for a daily decision, the day before for an
    intraday one (BE-20). Reads ``L`` from :func:`decision_interval`."""
    return core_known_through(as_datetime(as_of), _DECISION_INTERVAL.get())


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


def _ticker_events(lake: Any, ticker: str) -> tuple[CorporateAction, ...]:
    return LakeCorporateActions(lake).load([ticker]).for_ticker(ticker)


def get_last_n_bars(
    lake: Any,
    ticker: str,
    interval: Interval,
    as_of: Any,
    n: int,
    *,
    basis: PriceBasis = "adjusted",
) -> pd.DataFrame:
    """Return the last ``n`` bars of ``ticker`` at ``interval`` with
    timestamp ``<= as_of``, oldest first, re-indexed ``0..len-1``, on the
    given price ``basis`` (see the module docstring).

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
    df = df.reset_index(drop=True)
    lo = len(df) - len(df.iloc[-n:])
    if basis == "adjusted" and lo < len(df):
        return SeriesAdjustment.build(df, _ticker_events(lake, ticker)).apply(df, lo, len(df))
    return df.iloc[lo:].reset_index(drop=True)


# ---- shared bar cache -------------------------------------------------------

# Bounds wide enough to cover every bar a lake can hold.
_HISTORY_START = datetime(1900, 1, 1)
_HISTORY_END = datetime(2200, 1, 1)


class _Series:
    """One ticker's full bar history at one interval, oldest first, with
    the back-adjustment factors from its corporate actions."""

    __slots__ = ("adjustment", "closes", "frame", "interval", "timestamps")

    def __init__(
        self,
        frame: pd.DataFrame,
        events: tuple[CorporateAction, ...] = (),
        interval: Interval = Interval.DAY_1,
    ) -> None:
        self.frame = frame.reset_index(drop=True)
        self.interval = interval
        self.adjustment = SeriesAdjustment.build(self.frame, events)
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

    def visible_end(self, as_of: Any) -> int:
        """Number of bars complete at a decision on ``as_of`` (RS-03)."""
        return self.end_index(visible_cutoff(as_of, self.interval))

    def rows(self, lo: int, hi: int, basis: PriceBasis) -> pd.DataFrame:
        """Rows ``lo:hi`` (a copy), adjusted as of row ``hi - 1`` unless raw."""
        if basis == "raw":
            return self.frame.iloc[lo:hi].reset_index(drop=True)
        return self.adjustment.apply(self.frame, lo, hi)

    def close_slice(self, lo: int, hi: int, basis: PriceBasis) -> np.ndarray:
        """Closes ``lo:hi`` (a copy), adjusted as of row ``hi - 1`` unless raw."""
        if basis == "raw":
            return self.closes[lo:hi].copy()
        return self.adjustment.closes(self.closes, lo, hi)


class BarCache:
    """Read-through, look-ahead-safe bar cache over one lake.

    The first read of a ``(ticker, interval)`` pulls that series' whole
    history in one query (plus one for its corporate actions); every later
    read slices it in memory. A backtest asks for the trailing window on
    every bar, so this replaces one query per ticker per bar with one query
    per ticker per run.

    Look-ahead safety: the cache holds bars after ``as_of`` (it holds the
    whole series), so every accessor slices strictly on
    ``timestamp <= as_of`` (or ``<= end``) before returning anything, and
    returns copies so callers can't mutate the cached history. Adjusted
    reads (the default ``basis``) are adjusted as of the last returned bar,
    so events after the cutoff never leak in (see the module docstring).

    Rows written to the lake after a series is first read are not seen.
    Scope a cache to something short-lived (one strategy instance, which is
    one backtest trial or one production tick).
    """

    def __init__(self, lake: Any, *, weak: bool = False) -> None:
        # ``weak=True`` (used by :class:`LakeBarCaches`) keeps the cache from
        # pinning its lake alive when it is stored under that lake as a key.
        self._lake: Any = weakref.ref(lake) if weak else (lambda: lake)
        self._series: dict[tuple[str, str], _Series] = {}
        #: A point-in-time view that bounds every read (BL-49), or ``None``.
        self._cap: Any = None

    def clamped(self, view: Any) -> BarCache:
        """This cache seen through a point-in-time view: the same series
        (read once), but no read ever goes past ``view.bar_cutoff``."""
        out = copy.copy(self)
        out._cap = view
        return out

    def _limit(self, series: _Series, end: int) -> int:
        if self._cap is None:
            return end
        return min(end, series.end_index(self._cap.bar_cutoff(series.interval)))

    def _get(self, ticker: str, interval: Interval) -> _Series:
        key = (ticker, interval.code)
        series = self._series.get(key)
        if series is None:
            lake = self._lake()
            df = lake.get_bars(ticker, interval, start=_HISTORY_START, end=_HISTORY_END)
            if df is None or df.empty:
                series = _Series(pd.DataFrame() if df is None else df, interval=interval)
            else:
                series = _Series(df, _ticker_events(lake, ticker), interval)
            self._series[key] = series
        return series

    def last_n_bars(
        self,
        ticker: str,
        interval: Interval,
        as_of: Any,
        n: int,
        *,
        basis: PriceBasis = "adjusted",
    ) -> pd.DataFrame:
        """The last ``n`` bars complete at a decision on ``as_of`` (see the
        module doc), oldest first, re-indexed ``0..len-1``."""
        series = self._get(ticker, interval)
        if n <= 0 or series.frame.empty:
            return series.frame.iloc[0:0].copy()
        end = self._limit(series, series.visible_end(as_of))
        return series.rows(max(0, end - n), end, basis)

    def last_n_closes(
        self,
        ticker: str,
        interval: Interval,
        as_of: Any,
        n: int,
        *,
        basis: PriceBasis = "adjusted",
    ) -> np.ndarray:
        """Closes of :meth:`last_n_bars` as a float array (a copy)."""
        series = self._get(ticker, interval)
        if n <= 0:
            return np.array([], dtype=float)
        end = self._limit(series, series.visible_end(as_of))
        return series.close_slice(max(0, end - n), end, basis)

    def last_close(
        self, ticker: str, interval: Interval, as_of: Any
    ) -> tuple[datetime, float] | None:
        """``(timestamp, close)`` of the latest bar complete at a decision on
        ``as_of``, or ``None`` when there is none. The latest bar as of a
        cutoff is never adjusted, so this is the raw quote in either basis."""
        series = self._get(ticker, interval)
        end = self._limit(series, series.visible_end(as_of))
        if end == 0:
            return None
        ts = pd.Timestamp(series.timestamps[end - 1]).to_pydatetime()
        return ts, float(series.closes[end - 1])

    def bars_between(
        self,
        ticker: str,
        interval: Interval,
        start: Any,
        end: Any,
        *,
        basis: PriceBasis = "adjusted",
    ) -> pd.DataFrame:
        """Bars with ``start <= timestamp <= end``, oldest first, like
        ``lake.get_bars``; adjusted as of the last returned bar."""
        series = self._get(ticker, interval)
        if series.frame.empty:
            return series.frame.iloc[0:0].copy()
        lo = int(
            np.searchsorted(series.timestamps, np.datetime64(as_datetime(start), "us"), "left")
        )
        hi = self._limit(series, series.end_index(end))
        return series.rows(lo, hi, basis)


class LakeBarCaches:
    """One :class:`BarCache` per lake, held weakly.

    Strategies keep one of these per instance: the lab's permutation and
    perturbation tests hand the same strategy different lakes, which must
    never share bars. A lake that can't be weakly referenced gets a fresh
    (uncached, but correct) :class:`BarCache` on every call.
    """

    def __init__(self) -> None:
        self._caches: weakref.WeakKeyDictionary[Any, BarCache] = weakref.WeakKeyDictionary()
        self._views: weakref.WeakKeyDictionary[Any, BarCache] = weakref.WeakKeyDictionary()
        self._sessions: weakref.WeakKeyDictionary[Any, BarCache] = weakref.WeakKeyDictionary()

    def for_lake(self, lake: Any) -> BarCache:
        if isinstance(lake, PointInTimeLake):
            # One cache per session (read once per run), clamped to each
            # view; the clamped cache is kept per view, so memos keyed by the
            # cache object hold within one decision. Keyed by the session,
            # not the raw lake: a new session (the live engine opens one per
            # bar close) must see the bars written since.
            view = self._views.get(lake)
            if view is None:
                session = lake.pit_session
                base = self._sessions.get(session)
                if base is None:
                    try:
                        base = BarCache(session.lake, weak=True)
                    except TypeError:  # a lake that can't be weakly referenced
                        base = BarCache(session.lake)
                    self._sessions[session] = base
                view = base.clamped(lake)
                self._views[lake] = view
            return view
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


def memo_scope(lake: Any) -> Any:
    """The key for a per-lake memo of values stamped by bar (a signal on
    bar ``t``). Behind a point-in-time lake it is the run's session, so the
    memo outlives one decision's view: a value for bar ``t`` was computed
    at a decision on or after ``t`` and on or before today, so it never
    holds anything past today's decision. Memos of whole histories must key
    on the lake itself (a view), never on this."""
    return lake.pit_session if isinstance(lake, PointInTimeLake) else lake


# ---- shared decide helpers (RS-37) ------------------------------------------


def long_only_decide(
    strategy_id: str,
    target: str,
    allocation: float,
    my_picks: Sequence[tuple[float, str]],
    portfolio: Portfolio,
    prices: Mapping[str, float],
    as_of: Any,
) -> list[Order]:
    """Buy ``allocation`` of cash when picked and flat; sell everything when
    not picked and holding."""
    price = prices.get(target)
    holding = portfolio.positions.get(target, 0.0)
    picked = any(t == target for _, t in my_picks)
    if picked and price and price > 0 and holding <= 0 and portfolio.cash > 0:
        qty = portfolio.cash * float(allocation) / price
        if qty <= 0:
            return []
        side, quantity = "buy", qty
    elif not picked and holding > 0:
        side, quantity = "sell", holding
    else:
        return []
    return [
        Order(
            client_id=f"{strategy_id}:{side}:{target}:{iso(as_of)}",
            ticker=target,
            side=side,
            quantity=quantity,
            order_type="market",
            strategy_id=strategy_id,
        )
    ]


def close_all_positions(strategy_id: str, portfolio: Portfolio, as_of: Any) -> list[Order]:
    """A market close of every position: longs sold, shorts covered (a
    wrapper's risk-off exit, BE-14). For a long-only book it is
    :func:`sell_all_longs` with each order marked as a close."""
    out: list[Order] = []
    for ticker, qty in portfolio.positions.items():
        if qty == 0:
            continue
        side = "sell" if qty > 0 else "buy"
        token = "sell" if qty > 0 else "cover"
        out.append(
            Order(
                client_id=f"{strategy_id}:{token}:{ticker}:{iso(as_of)}",
                ticker=ticker,
                side=side,
                quantity=abs(qty),
                order_type="market",
                strategy_id=strategy_id,
                position_effect="close",
            )
        )
    return out


def closing_orders(orders: Sequence[Order], portfolio: Portfolio) -> list[Order]:
    """The legs of ``orders`` that reduce a position, split at zero against
    ``portfolio``: a risk-off block keeps covers and sells of longs and
    drops every order that opens or adds to one (BE-14)."""
    from stonks.execution.orders import classify_all

    return [o for o in classify_all(orders, portfolio.positions) if o.position_effect == "close"]


def sell_all_longs(strategy_id: str, portfolio: Portfolio, as_of: Any) -> list[Order]:
    """A market sell of every long position (a wrapper's risk-off exit)."""
    return [
        Order(
            client_id=f"{strategy_id}:sell:{ticker}:{iso(as_of)}",
            ticker=ticker,
            side="sell",
            quantity=qty,
            order_type="market",
            strategy_id=strategy_id,
        )
        for ticker, qty in portfolio.positions.items()
        if qty > 0
    ]


def close_all(strategy_id: str, portfolio: Portfolio, as_of: Any) -> list[Order]:
    """A market close of every position: longs sold, shorts covered (a
    wrapper's risk-off exit, BE-14)."""
    return [
        Order(
            client_id=f"{strategy_id}:{'sell' if qty > 0 else 'cover'}:{ticker}:{iso(as_of)}",
            ticker=ticker,
            side="sell" if qty > 0 else "buy",
            quantity=abs(qty),
            order_type="market",
            strategy_id=strategy_id,
            position_effect="close",
        )
        for ticker, qty in portfolio.positions.items()
        if abs(qty) > 1e-12
    ]


def split_effects(orders: Sequence[Order], portfolio: Portfolio) -> list[Order]:
    """``orders`` split at zero against the book, each leg with its position
    effect: the part that reduces a position is ``close``, the rest
    ``open``. An order with an effect already passes as it is."""
    held = dict(portfolio.positions)
    out: list[Order] = []
    for order in orders:
        if order.position_effect is not None:
            out.append(order)
            continue
        qty = held.get(order.ticker, 0.0)
        closable = max(-qty, 0.0) if order.side == "buy" else max(qty, 0.0)
        close = min(order.quantity, closable)
        rest = order.quantity - close
        if close > 1e-12:
            out.append(dataclasses.replace(order, quantity=close, position_effect="close"))
        if rest > 1e-12:
            cid = f"{order.client_id}:open" if close > 1e-12 else order.client_id
            out.append(
                dataclasses.replace(order, client_id=cid, quantity=rest, position_effect="open")
            )
        held[order.ticker] = qty + (order.quantity if order.side == "buy" else -order.quantity)
    return out


def closes_only(orders: Sequence[Order], portfolio: Portfolio) -> list[Order]:
    """The closing legs of ``orders`` (a risk-off wrapper blocks new risk,
    long or short, and lets exits through)."""
    return [o for o in split_effects(orders, portfolio) if o.position_effect == "close"]
