"""Causal price-extreme detectors: rolling window, directional change (fixed
percentage and ATR threshold), hierarchical market-structure levels and
perceptually important points (PIPs).

Ported from the MIT-licensed research of neurotrader888:

- ``TechnicalAnalysisAutomation`` (``rolling_window.py``,
  ``directional_change.py``, ``perceptually_important.py``)
  https://github.com/neurotrader888/TechnicalAnalysisAutomation
- ``market-structure`` (``atr_directional_change.py``,
  ``hierarchical_extremes.py``)
  https://github.com/neurotrader888/market-structure

Both repositories are MIT licensed (Copyright (c) neurotrader888). This is an
independent re-implementation of the algorithms, not a copy of the code.

Look-ahead contract: every extreme carries ``conf_index``, the first bar at
which it can be known, next to ``ext_index``, the bar it sits at. A consumer
standing at bar ``i`` may only use extremes with ``conf_index <= i``
(:func:`visible`). Each detector is causal: running it on ``data[:n]``
reports exactly the extremes of the full run with ``conf_index < n``.

Deliberate deviations from the originals:

- ``rw_extremes`` confirms an extreme as soon as its full ``±order``
  neighbourhood exists (``conf_index = ext_index + order`` from bar
  ``2 * order`` on). The original skipped bar ``2 * order`` itself.
- The ATR directional change reads a precomputed simple-mean ATR
  (:func:`stonks.features.indicators.atr` with ``method="sma"``) instead of
  keeping its own running true-range sum; the values are the same.
- ``HierarchicalExtremes`` accepts base-level extremes from any detector
  (:meth:`HierarchicalExtremes.add_base_extreme`); the original was wired
  to the ATR directional change only. Timestamps are not tracked (bar
  indices only).
- ``find_pips`` stops early (returning fewer points) when no interior point
  is left to insert, instead of inserting index ``-1`` as the original did
  in that corner case.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, NamedTuple

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from stonks.features.indicators import atr

ExtremeKind = Literal["top", "bottom"]

__all__ = [
    "ATRDirectionalChange",
    "DirectionalChange",
    "Extreme",
    "ExtremeKind",
    "HierarchicalExtremes",
    "atr_directional_change",
    "directional_change",
    "find_pips",
    "hierarchical_level_prices",
    "rw_extremes",
    "visible",
]


class Extreme(NamedTuple):
    """A local top or bottom.

    ``conf_index`` is the bar on which the extreme becomes known;
    ``ext_index`` is the bar it sits at (``ext_index <= conf_index``).
    """

    conf_index: int
    ext_index: int
    price: float
    kind: ExtremeKind


def visible(extremes: Sequence[Extreme], i: int) -> list[Extreme]:
    """The extremes a consumer standing at bar ``i`` may use."""
    return [e for e in extremes if e.conf_index <= i]


# ---- rolling window --------------------------------------------------------


def rw_extremes(close: np.ndarray, order: int) -> list[Extreme]:
    """Rolling-window local extremes of ``close``.

    Bar ``k`` is a top when ``close[k]`` is ``>=`` every close within
    ``order`` bars on either side (a bottom when ``<=``). It is confirmed at
    ``k + order``, the first bar at which the right-hand side exists.
    Returned in confirmation order (a top before a bottom on the same bar).
    """
    if order < 1:
        raise ValueError(f"order must be >= 1, got {order}")
    close = np.asarray(close, dtype=float)
    width = 2 * order + 1
    if len(close) < width:
        return []
    windows = sliding_window_view(close, width)
    centre = close[order : len(close) - order]
    tops = np.flatnonzero(centre >= windows.max(axis=1)) + order
    bottoms = np.flatnonzero(centre <= windows.min(axis=1)) + order
    out = [Extreme(int(k) + order, int(k), float(close[k]), "top") for k in tops]
    out += [Extreme(int(k) + order, int(k), float(close[k]), "bottom") for k in bottoms]
    out.sort(key=lambda e: (e.conf_index, e.kind != "top"))
    return out


# ---- directional change ----------------------------------------------------


class DirectionalChange:
    """Incremental percentage zig-zag.

    While looking for a top, track the running max of highs; the top is
    confirmed on the first bar whose close is below ``max * (1 - sigma)``.
    Symmetrically for bottoms with lows and ``min * (1 + sigma)``. Starts out
    looking for a top. :meth:`update` takes one bar's scalars, so it cannot
    peek ahead.
    """

    def __init__(self, sigma: float) -> None:
        if sigma <= 0:
            raise ValueError(f"sigma must be > 0, got {sigma}")
        self.sigma = float(sigma)
        self._seek_top = True
        self._ext = np.nan
        self._ext_i = 0
        self._started = False

    def update(self, i: int, high: float, low: float, close: float) -> Extreme | None:
        if not self._started:
            self._started = True
            self._ext, self._ext_i = float(high), i
        if self._seek_top:
            if high > self._ext:
                self._ext, self._ext_i = float(high), i
            elif close < self._ext * (1.0 - self.sigma):
                found = Extreme(i, self._ext_i, self._ext, "top")
                self._seek_top = False
                self._ext, self._ext_i = float(low), i
                return found
        else:
            if low < self._ext:
                self._ext, self._ext_i = float(low), i
            elif close > self._ext * (1.0 + self.sigma):
                found = Extreme(i, self._ext_i, self._ext, "bottom")
                self._seek_top = True
                self._ext, self._ext_i = float(high), i
                return found
        return None


def directional_change(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, sigma: float
) -> list[Extreme]:
    """All extremes of :class:`DirectionalChange` over the arrays."""
    dc = DirectionalChange(sigma)
    out = []
    for i in range(len(close)):
        found = dc.update(i, float(high[i]), float(low[i]), float(close[i]))
        if found is not None:
            out.append(found)
    return out


class ATRDirectionalChange:
    """Incremental zig-zag with an ATR threshold.

    While looking for a top, track the running max of highs; the top is
    confirmed on the first bar whose low is below ``max - atr``. Bottoms
    mirror that with lows and ``high > min + atr``. Bars whose ATR is not
    defined yet (``NaN``, the warm-up) are skipped; tracking starts on the
    first bar with a defined ATR.
    """

    def __init__(self) -> None:
        self._seek_top = True
        self._max = np.nan
        self._min = np.nan
        self._max_i = 0
        self._min_i = 0
        self._started = False

    def update(self, i: int, high: float, low: float, atr_value: float) -> Extreme | None:
        if not np.isfinite(atr_value):
            return None
        if not self._started:
            self._started = True
            self._max, self._min = float(high), float(low)
            self._max_i = self._min_i = i
        if self._seek_top:
            if high > self._max:
                self._max, self._max_i = float(high), i
            elif low < self._max - atr_value:
                found = Extreme(i, self._max_i, self._max, "top")
                self._seek_top = False
                self._min, self._min_i = float(low), i
                return found
        else:
            if low < self._min:
                self._min, self._min_i = float(low), i
            elif high > self._min + atr_value:
                found = Extreme(i, self._min_i, self._min, "bottom")
                self._seek_top = True
                self._max, self._max_i = float(high), i
                return found
        return None


def _sma_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, lookback: int) -> np.ndarray:
    return atr(
        pd.Series(np.asarray(high, dtype=float)),
        pd.Series(np.asarray(low, dtype=float)),
        pd.Series(np.asarray(close, dtype=float)),
        lookback,
        method="sma",
    ).to_numpy()


def atr_directional_change(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, atr_lookback: int
) -> list[Extreme]:
    """All extremes of :class:`ATRDirectionalChange` with a simple-mean ATR
    over ``atr_lookback`` bars (causal: the ATR at bar ``i`` uses bars
    ``<= i``)."""
    a = _sma_atr(high, low, close, atr_lookback)
    dc = ATRDirectionalChange()
    out = []
    for i in range(len(a)):
        found = dc.update(i, float(high[i]), float(low[i]), float(a[i]))
        if found is not None:
            out.append(found)
    return out


# ---- hierarchical extremes -------------------------------------------------


def _beats(x: float, y: float, kind: ExtremeKind) -> bool:
    """``x`` is a more extreme price than ``y`` for extremes of ``kind``."""
    return x > y if kind == "top" else x < y


def _opposite(kind: ExtremeKind) -> ExtremeKind:
    return "bottom" if kind == "top" else "top"


class HierarchicalExtremes:
    """Market-structure levels built from alternating base extremes.

    Level 0 is the base zig-zag. A level-``L`` extreme is promoted to level
    ``L + 1`` when it beats the neighbouring same-type level-``L`` extremes
    on both sides (a high above the previous and the next level-``L``
    highs). It becomes known when its right-hand neighbour is confirmed, so
    the promoted copy takes that confirmation bar. When the new level-``L+1``
    extreme has the same type as the last one on that level, the most
    extreme opposite point between them (lowest low between two highs) is
    promoted first, keeping every level alternating.
    """

    def __init__(self, levels: int = 5) -> None:
        if levels < 1:
            raise ValueError(f"levels must be >= 1, got {levels}")
        self.levels = levels
        self.extremes: list[list[Extreme]] = [[] for _ in range(levels)]
        self._dc = ATRDirectionalChange()

    # -- feeding ---------------------------------------------------------

    def update(self, i: int, high: float, low: float, atr_value: float) -> None:
        """Feed one bar to the built-in ATR directional change."""
        found = self._dc.update(i, high, low, atr_value)
        if found is not None:
            self.add_base_extreme(found)

    def add_base_extreme(self, ext: Extreme) -> None:
        """Append a confirmed level-0 extreme (must alternate with the last)."""
        base = self.extremes[0]
        if base and base[-1].kind == ext.kind:
            raise ValueError("base extremes must alternate between tops and bottoms")
        base.append(ext)
        self._promote(0, ext.conf_index)

    def _promote(self, level: int, conf_i: int) -> None:
        if level >= self.levels - 1:
            return
        lvl = self.extremes[level]
        n = len(lvl) - 1
        # need two earlier same-type extremes (n-2 is the candidate, n-4 its
        # left neighbour)
        if n < 4:
            return
        kind = lvl[n].kind
        cand = lvl[n - 2]
        if not _beats(cand.price, lvl[n].price, kind):
            return
        upper = self.extremes[level + 1]
        last_up = upper[-1] if upper else None
        if (
            last_up is not None
            and last_up.kind != kind
            and not _beats(cand.price, last_up.price, kind)
        ):
            return
        for k in range(n - 4, -1, -2):
            prior = lvl[k]
            if _beats(prior.price, cand.price, kind):
                return
            if last_up is not None and prior.ext_index <= last_up.ext_index:
                break
            if prior.price == cand.price:
                cand = prior  # equal-priced run: the promoted point is its first bar
            else:
                break

        if last_up is not None and last_up.kind == kind:
            between = [
                e
                for e in lvl
                if e.kind != kind and last_up.ext_index < e.ext_index < cand.ext_index
            ]
            if not between:  # pragma: no cover - alternation guarantees one
                return
            pivot = between[0]
            for e in between[1:]:
                if _beats(e.price, pivot.price, _opposite(kind)):
                    pivot = e
            upper.append(pivot._replace(conf_index=conf_i))
            self._promote(level + 1, conf_i)

        upper.append(cand._replace(conf_index=conf_i))
        self._promote(level + 1, conf_i)

    # -- queries ---------------------------------------------------------

    def _last(self, level: int, kind: ExtremeKind, lag: int) -> Extreme | None:
        lvl = self.extremes[level]
        if not lvl:
            return None
        offset = 0 if lvl[-1].kind == kind else 1
        pos = 2 * lag + offset
        if pos >= len(lvl):
            return None
        return lvl[-(pos + 1)]

    def level_high(self, level: int, lag: int = 0) -> Extreme | None:
        """Last confirmed high at ``level`` (``lag`` highs back)."""
        return self._last(level, "top", lag)

    def level_low(self, level: int, lag: int = 0) -> Extreme | None:
        """Last confirmed low at ``level`` (``lag`` lows back)."""
        return self._last(level, "bottom", lag)

    def level_high_price(self, level: int, lag: int = 0) -> float:
        ext = self.level_high(level, lag)
        return np.nan if ext is None else ext.price

    def level_low_price(self, level: int, lag: int = 0) -> float:
        ext = self.level_low(level, lag)
        return np.nan if ext is None else ext.price


def hierarchical_level_prices(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    atr_lookback: int,
    level: int,
    levels: int = 5,
) -> tuple[np.ndarray, np.ndarray]:
    """Per-bar ``(last level high, last level low)`` prices as known at the
    close of each bar (``NaN`` until the level has one), from an ATR
    directional-change base."""
    if not 0 <= level < levels:
        raise ValueError(f"level must be in [0, {levels}), got {level}")
    a = _sma_atr(high, low, close, atr_lookback)
    he = HierarchicalExtremes(levels)
    n = len(a)
    hi = np.full(n, np.nan)
    lo = np.full(n, np.nan)
    for i in range(n):
        he.update(i, float(high[i]), float(low[i]), float(a[i]))
        hi[i] = he.level_high_price(level)
        lo[i] = he.level_low_price(level)
    return hi, lo


# ---- perceptually important points -----------------------------------------


def find_pips(
    data: np.ndarray, n_pips: int, dist_measure: int = 3
) -> tuple[list[int], list[float]]:
    """Perceptually important points of ``data``.

    Starts from the two endpoints and repeatedly inserts the point farthest
    from the line joining its two neighbouring PIPs. ``dist_measure``: 1 =
    euclidean (sum of distances to both neighbours), 2 = perpendicular
    distance to the line, 3 = vertical distance to the line. Returns
    ``(indices, values)`` sorted by index; fewer than ``n_pips`` when the
    series runs out of interior points.
    """
    if dist_measure not in (1, 2, 3):
        raise ValueError(f"dist_measure must be 1, 2 or 3, got {dist_measure}")
    data = np.asarray(data, dtype=float)
    n = len(data)
    if n == 0:
        return [], []
    xs = [0] if n == 1 else [0, n - 1]
    while len(xs) < n_pips:
        best_d = -1.0
        best_i = -1
        for left, right in zip(xs, xs[1:], strict=False):
            if right - left < 2:
                continue
            idx = np.arange(left + 1, right)
            y = data[idx]
            yl, yr = data[left], data[right]
            if dist_measure == 1:
                d = np.hypot(idx - left, y - yl) + np.hypot(right - idx, y - yr)
            else:
                slope = (yr - yl) / (right - left)
                d = np.abs(yl + slope * (idx - left) - y)
                if dist_measure == 2:
                    d = d / np.sqrt(slope * slope + 1.0)
            j = int(np.argmax(d))
            if d[j] > best_d:
                best_d, best_i = float(d[j]), int(idx[j])
        if best_i < 0:
            break
        xs.append(best_i)
        xs.sort()
    return xs, [float(data[i]) for i in xs]
