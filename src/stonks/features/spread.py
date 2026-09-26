"""Bid-ask spread estimators from daily OHLC: pure and causal (BL-31).

Both estimators take pandas Series (one instrument) or DataFrames (one
column per instrument) of **adjusted** high / low / close prices and
return the **full** proportional spread (``0.001`` = 10 bps quoted spread)
aligned to the input index. The value at bar ``t`` uses bars ``<= t``
only; it is NaN until the window is full. Shift by one bar before using it
to price a fill inside bar ``t`` (the backtest engine does). Use
:func:`half_spread_bps` for the cost model's unit.

- :func:`corwin_schultz` — Corwin & Schultz (2012). Each pair of bars
  ``(t-1, t)`` gives
  ``beta = ln(H_{t-1}/L_{t-1})^2 + ln(H_t/L_t)^2``,
  ``gamma = ln(max(H_{t-1}, H_t) / min(L_{t-1}, L_t))^2``,
  ``alpha = (sqrt(2 beta) - sqrt(beta)) / (3 - 2 sqrt 2) - sqrt(gamma / (3 - 2 sqrt 2))``
  and ``S = 2 (e^alpha - 1) / (1 + e^alpha)``. Bar ``t``'s range is first
  shifted by the overnight gap when the prior close lies outside it (the
  paper's adjustment). Negative two-bar estimates are set to 0, then the
  last ``n`` are averaged.
- :func:`abdi_ranaldo` — Abdi & Ranaldo (2017), from close and mid-range
  ``eta = (ln H + ln L) / 2``:
  ``S^2 = 4 mean((c_{t-1} - eta_{t-1}) (c_{t-1} - eta_t))`` over the last
  ``n`` pairs, with a negative mean floored at 0 before the square root
  (the paper's "monthly" form).

Both assume continuous trading inside a bar; on thin or sparsely sampled
instruments they are noisy, which is why the cost model clips them to a
per-asset-class floor and a cap.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

type Frame = pd.Series | pd.DataFrame

_BPS = 10_000.0
_K = 3.0 - 2.0 * math.sqrt(2.0)


def _check_window(n: int) -> None:
    if n < 1:
        raise ValueError(f"window n must be >= 1, got {n}")


def corwin_schultz_pairs(high: Frame, low: Frame, close: Frame | None = None) -> Frame:
    """The two-bar Corwin-Schultz spread for each pair ``(t-1, t)``, indexed
    at ``t`` and floored at 0 (NaN at the first bar). With ``close`` the
    bar-``t`` range is shifted by the overnight gap first."""
    high_t, low_t = high, low
    if close is not None:
        prev_close = close.shift(1)
        up = (prev_close - high).clip(lower=0.0).fillna(0.0)  # gap down: raise H, L
        down = (low - prev_close).clip(lower=0.0).fillna(0.0)  # gap up: lower H, L
        high_t = high + up - down
        low_t = low + up - down
    high_p, low_p = high.shift(1), low.shift(1)
    beta = np.log(high_p / low_p) ** 2 + np.log(high_t / low_t) ** 2
    gamma = np.log(np.maximum(high_p, high_t) / np.minimum(low_p, low_t)) ** 2
    alpha = (np.sqrt(2.0 * beta) - np.sqrt(beta)) / _K - np.sqrt(gamma / _K)
    spread = 2.0 * (np.exp(alpha) - 1.0) / (1.0 + np.exp(alpha))
    return spread.clip(lower=0.0)


def corwin_schultz(high: Frame, low: Frame, close: Frame | None = None, n: int = 20) -> Frame:
    """Rolling mean of the last ``n`` two-bar Corwin-Schultz estimates
    (``n + 1`` bars); full proportional spread."""
    _check_window(n)
    return corwin_schultz_pairs(high, low, close).rolling(n, min_periods=n).mean()


def abdi_ranaldo(high: Frame, low: Frame, close: Frame, n: int = 20) -> Frame:
    """Abdi-Ranaldo spread over the last ``n`` pairs of bars (``n + 1``
    bars); full proportional spread."""
    _check_window(n)
    eta = (np.log(high) + np.log(low)) / 2.0
    c_prev = np.log(close).shift(1)
    products = 4.0 * (c_prev - eta.shift(1)) * (c_prev - eta)
    mean = products.rolling(n, min_periods=n).mean()
    return mean.clip(lower=0.0) ** 0.5


def half_spread_bps(spread: Frame | float) -> Frame | float:
    """Full proportional spread -> half-spread in basis points."""
    return spread / 2.0 * _BPS
