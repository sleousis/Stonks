"""Pairs features: hedge ratio, spread z-scores, half-life and the
entry/exit state machine of a mean-reverting pair (Chan, *Algorithmic
Trading* ch. 2-4; Vidyamurthy, *Pairs Trading*; Gatev, Goetzmann and
Rouwenhorst 2006).

Everything works on log prices of two tickers, oldest first, and uses only
the rows it is given, so a caller that passes bars up to ``as_of`` never
looks ahead.

- :func:`hedge_ratio`: OLS slope ``beta`` of ``log A`` on ``log B``.
- :func:`spread_zscores`: ``s = log A - beta log B``, standardised by the
  window's own mean and standard deviation.
- :func:`half_life`: bars for a shock to the spread to halve, from the
  AR(1) fit ``ds_t = a + b s_{t-1}``: ``-ln 2 / ln(1 + b)``. ``None`` when ``b >= 0``
  (the spread does not revert).
- :func:`pair_state`: replay the z-scores from flat: enter at
  ``|z| >= entry_z`` (short the rich leg, long the cheap one), exit at
  ``|z| <= exit_z``, and stop out at ``|z| >= stop_z``. The state is ``+1``
  (long A, short B), ``-1`` (short A, long B) or ``0``.
"""

from __future__ import annotations

import math

import numpy as np

PairState = int


def hedge_ratio(log_a: np.ndarray, log_b: np.ndarray) -> float | None:
    """OLS slope of ``log_a`` on ``log_b`` (with an intercept)."""
    a = np.asarray(log_a, dtype=float)
    b = np.asarray(log_b, dtype=float)
    if len(a) != len(b) or len(a) < 3:
        return None
    var = float(np.var(b, ddof=1))
    if not var > 0:
        return None
    beta = float(np.cov(a, b, ddof=1)[0, 1] / var)
    return beta if math.isfinite(beta) else None


def spread(log_a: np.ndarray, log_b: np.ndarray, beta: float) -> np.ndarray:
    return np.asarray(log_a, dtype=float) - beta * np.asarray(log_b, dtype=float)


def spread_zscores(values: np.ndarray) -> np.ndarray | None:
    """``values`` standardised by their own mean and sample std."""
    s = np.asarray(values, dtype=float)
    if len(s) < 3:
        return None
    sd = float(np.std(s, ddof=1))
    if not sd > 0:
        return None
    return (s - float(np.mean(s))) / sd


def half_life(values: np.ndarray) -> float | None:
    """Bars for a shock to ``values`` to halve (AR(1) fit, see module doc)."""
    s = np.asarray(values, dtype=float)
    if len(s) < 3:
        return None
    lagged, delta = s[:-1], np.diff(s)
    var = float(np.var(lagged, ddof=1))
    if not var > 0:
        return None
    b = float(np.cov(delta, lagged, ddof=1)[0, 1] / var)
    if not b < 0 or not math.isfinite(b):
        return None
    return -math.log(2.0) / math.log1p(b) if b > -1.0 else 0.0


def pair_state(z: np.ndarray, entry_z: float, exit_z: float, stop_z: float) -> PairState:
    """The pair's position after replaying ``z`` from flat (module doc)."""
    if not 0.0 <= exit_z < entry_z < stop_z:
        raise ValueError("need 0 <= exit_z < entry_z < stop_z")
    state = 0
    for value in np.asarray(z, dtype=float):
        if not math.isfinite(value):
            continue
        if state == 0:
            if entry_z <= abs(value) < stop_z:
                state = -1 if value > 0 else 1
        elif abs(value) <= exit_z or abs(value) >= stop_z:
            state = 0
    return state
