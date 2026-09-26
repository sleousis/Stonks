"""Point-in-time back-adjustment of bar history for splits and dividends.

Signals read price *history*; a 10:1 split or a large dividend would look
like a crash in raw prices. This module computes, for one ticker's bar
series, the factor that turns raw OHLC into adjusted OHLC.

Policy (causal, "as of" adjustment)
-----------------------------------
History read **as of** bar ``d`` is adjusted only by events effective on or
before ``d``: bar ``t <= d`` is multiplied by the product of the factors
of every event effective in ``(t, d]``. Events after ``d`` never touch the
result, so a backtest sees exactly what a trader could have computed that
day, and the latest bar as of ``d`` is always its raw price (so adjusted
levels line up with the raw prices orders fill at).

Implementation: one reverse cumulative product ``F[i]`` = product of the
factors of events effective after bar ``i`` over the whole series; the
series as of the bar at index ``hi - 1`` is ``raw[lo:hi] * F[lo:hi] /
F[hi - 1]``. Each event's factor depends only on the close of the bar
before its ex-date, so this is causal; building ``F`` is O(n) once per
series and each as-of slice is O(window).

Factor sources, never mixed (mixing would double-adjust, since a vendor's
``adj_close`` already includes splits *and* dividends):

1. **Corporate-action events** when the ticker has any: a split of ratio
   ``r`` scales earlier prices by ``1 / r`` (and volume by ``r``); a cash
   dividend ``D`` scales earlier prices by ``1 - D / close_prev``, with
   ``close_prev`` the raw close of the last bar before the ex-date.
2. Otherwise the vendor's ``adj_close / close`` ratio (volume untouched,
   since the ratio can't separate splits from dividends). Missing ratios
   take the next known one. A ratio that never changes is the identity.

An event takes effect on the first bar dated on or after its ex-date
(intraday bars included), matching the backtest engine's accounting.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from stonks.core.corporate_actions import CorporateAction, Split

_PRICE_COLS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class SeriesAdjustment:
    """Back-adjustment factors for one bar series (``None`` = identity)."""

    price: np.ndarray | None = None
    volume: np.ndarray | None = None

    @property
    def is_identity(self) -> bool:
        return self.price is None and self.volume is None

    @classmethod
    def build(cls, frame: pd.DataFrame, events: Sequence[CorporateAction]) -> SeriesAdjustment:
        """Factors for ``frame`` (one ticker, oldest first, with ``timestamp``
        and ``close``; ``adj_close`` optional) from ``events``, falling back
        to ``adj_close`` when there are none."""
        if frame is None or frame.empty:
            return cls()
        closes = frame["close"].to_numpy(dtype=float)
        if events:
            timestamps = pd.to_datetime(frame["timestamp"]).to_numpy(dtype="datetime64[us]")
            return _from_events(timestamps, closes, events)
        if "adj_close" in frame.columns:
            return cls(price=_from_adj_close(closes, frame["adj_close"].to_numpy(dtype=float)))
        return cls()

    def closes(self, raw: np.ndarray, lo: int, hi: int) -> np.ndarray:
        """Adjusted ``raw[lo:hi]`` as of bar ``hi - 1`` (a copy)."""
        out = np.array(raw[lo:hi], dtype=float)
        if self.price is not None and hi > lo:
            out *= self.price[lo:hi] / self.price[hi - 1]
        return out

    def apply(self, frame: pd.DataFrame, lo: int, hi: int) -> pd.DataFrame:
        """Rows ``lo:hi`` of ``frame`` with OHLC (and volume) adjusted as of
        row ``hi - 1``; re-indexed ``0..len-1``. ``frame`` is not modified."""
        out = frame.iloc[lo:hi].reset_index(drop=True)
        if hi <= lo or self.is_identity:
            return out
        if self.price is not None:
            scale = self.price[lo:hi] / self.price[hi - 1]
            for col in _PRICE_COLS:
                if col in out.columns:
                    out[col] = out[col].to_numpy(dtype=float) * scale
        if self.volume is not None and "volume" in out.columns:
            out["volume"] = out["volume"].to_numpy(dtype=float) * (
                self.volume[lo:hi] / self.volume[hi - 1]
            )
        return out


def _from_events(
    timestamps: np.ndarray, closes: np.ndarray, events: Sequence[CorporateAction]
) -> SeriesAdjustment:
    n = len(closes)
    price_steps = np.ones(n + 1)
    volume_steps = np.ones(n + 1)
    touched_price = touched_volume = False
    for event in events:
        # first bar dated on or after the ex-date
        idx = int(np.searchsorted(timestamps, np.datetime64(event.ex_date, "us"), side="left"))
        if idx == 0:
            continue  # no bar before the ex-date to adjust
        if isinstance(event, Split):
            price_steps[idx] /= event.ratio
            volume_steps[idx] *= event.ratio
            touched_price = touched_volume = True
        else:
            prev_close = closes[idx - 1]
            if not prev_close > event.amount:
                continue  # nonsensical (dividend >= price); ignore
            price_steps[idx] *= 1.0 - event.amount / prev_close
            touched_price = True
    return SeriesAdjustment(
        price=_suffix_product(price_steps) if touched_price else None,
        volume=_suffix_product(volume_steps) if touched_volume else None,
    )


def _suffix_product(steps: np.ndarray) -> np.ndarray:
    """``F[i] = prod(steps[i + 1:])`` for ``i`` in ``0..len(steps) - 2``."""
    return np.cumprod(steps[::-1])[::-1][1:]


def _from_adj_close(closes: np.ndarray, adj: np.ndarray) -> np.ndarray | None:
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where((closes > 0) & (adj > 0), adj / closes, np.nan)
    series = pd.Series(ratio).bfill().ffill()
    if series.isna().all():
        return None
    values = series.to_numpy(dtype=float)
    if np.all(values == values[0]):
        return None
    return values
