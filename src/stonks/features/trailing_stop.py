"""Volatility-scaled trailing stops, replayed statelessly from bars (BL-40).

A trade entered at the close of bar ``e`` carries a stop

    level_t = max(level_{t-1}, HWM_t - k * ATR_t),   HWM_t = max(close_e..close_t)

which never moves down (Wilcox & Crittenden's and Clenow's ATR trailing
stop; Carver's volatility-scaled exit). The trade is stopped on the first
bar ``t > e`` whose close is below ``level_{t-1}``, the stop in force while
bar ``t`` trades; the decision is made at that close and fills later, like
every other signal. Bars before a valid ATR carry no stop.

:func:`replay_trades` rebuilds the whole trade history from arrays of bars
and entry / hold flags, so a strategy needs no state between calls: the
position at ``as_of`` is whatever the replay over bars ``<= as_of`` says.
Everything here is causal: a trade's entry and exit depend only on bars up
to them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

ExitReason = Literal["stop", "signal"]


@dataclass(frozen=True)
class Trade:
    """Bar indices of one replayed trade. ``exit`` is the bar whose close
    triggered the exit (``None`` while the trade is still open)."""

    entry: int
    exit: int | None
    reason: ExitReason | None


def trailing_stop_levels(closes: np.ndarray, atrs: np.ndarray, k: float) -> np.ndarray:
    """Stop level after each bar of a trade entered at index 0 (NaN until the
    first bar with a valid ATR)."""
    closes = np.asarray(closes, dtype=float)
    atrs = np.asarray(atrs, dtype=float)
    # fmax skips a missing close; maximum would spread it and freeze the stop
    candidates = np.fmax.accumulate(closes) - k * atrs
    candidates = np.where(np.isfinite(candidates), candidates, -np.inf)
    levels = np.maximum.accumulate(candidates)
    return np.where(np.isneginf(levels), np.nan, levels)


def stop_exit(closes: np.ndarray, atrs: np.ndarray, entry: int, k: float) -> int | None:
    """The first bar after ``entry`` whose close is below the previous bar's
    stop level, or ``None`` if the stop is never hit."""
    levels = trailing_stop_levels(closes[entry:], atrs[entry:], k)
    below = np.asarray(closes[entry + 1 :], dtype=float) < levels[:-1]  # NaN compares False
    hits = np.flatnonzero(below)
    return int(entry + 1 + hits[0]) if hits.size else None


def replay_trades(
    closes: np.ndarray,
    atrs: np.ndarray,
    entries: np.ndarray,
    k: float,
    *,
    holds: np.ndarray | None = None,
    cooldown_bars: int = 0,
) -> list[Trade]:
    """Replay long trades over bars ``0..n-1``.

    - Flat, a trade opens on the first allowed bar where ``entries`` is True.
    - Open, it closes on the trailing stop (:func:`stop_exit`) or, when
      ``holds`` is given, on the first later bar where ``holds`` is False,
      whichever comes first (the stop wins a tie).
    - After a stop exit on bar ``x`` no entry is allowed before bar
      ``x + cooldown_bars + 1``; after a signal exit, from bar ``x + 1``.
    """
    closes = np.asarray(closes, dtype=float)
    atrs = np.asarray(atrs, dtype=float)
    entries = np.asarray(entries, dtype=bool)
    n = len(closes)
    if len(atrs) != n or len(entries) != n or (holds is not None and len(holds) != n):
        raise ValueError("closes, atrs, entries and holds must have the same length")
    if not k > 0:
        raise ValueError(f"k must be positive, got {k}")
    if cooldown_bars < 0:
        raise ValueError(f"cooldown_bars must be >= 0, got {cooldown_bars}")
    off = None if holds is None else ~np.asarray(holds, dtype=bool)

    trades: list[Trade] = []
    start = 0
    while start < n:
        candidates = np.flatnonzero(entries[start:])
        if not candidates.size:
            break
        entry = start + int(candidates[0])
        stop = stop_exit(closes, atrs, entry, k)
        signal = None
        if off is not None:
            offs = np.flatnonzero(off[entry + 1 :])
            signal = entry + 1 + int(offs[0]) if offs.size else None
        if stop is None and signal is None:
            trades.append(Trade(entry, None, None))
            break
        if signal is None or (stop is not None and stop <= signal):
            trades.append(Trade(entry, stop, "stop"))
            start = stop + cooldown_bars + 1  # type: ignore[operator]
        else:
            trades.append(Trade(entry, signal, "signal"))
            start = signal + 1
    return trades


def open_trade(trades: list[Trade]) -> Trade | None:
    """The trade still open at the last replayed bar, if any."""
    return trades[-1] if trades and trades[-1].exit is None else None
