"""A small toolkit of reusable feature-construction helpers.

**Not** a pipeline stage. Strategies own their own feature extraction. These
are just conveniences — any strategy is free to ignore them and compute
features inline.

The second half of this file collects standard technical-analysis and
statistical primitives — Hawkes-process volatility smoothing, ordinal-
pattern permutation entropy, a permutation-based time-reversibility
score, robust trendline fitting, and the Wald-Wolfowitz runs-test Z-
score — that higher-level strategies and survival tests compose.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def ttm(series: pd.Series) -> pd.Series:
    """Trailing twelve months — 4-period rolling sum (assumes quarterly data)."""
    return series.rolling(window=4, min_periods=4).sum()


def pct_change_n_days(series: pd.Series, n: int) -> pd.Series:
    """N-step percentage change. Same as ``series.pct_change(n)`` but explicit."""
    return series.pct_change(periods=n)


def rolling_zscore(series: pd.Series, window: int) -> pd.Series:
    """Rolling (x - mean) / std with the given window."""
    mean = series.rolling(window=window, min_periods=window).mean()
    std = series.rolling(window=window, min_periods=window).std()
    return (series - mean) / std.replace(0, pd.NA)


def trailing_return(prices: pd.Series, n_days: int) -> pd.Series:
    """Return of ``prices`` over the last ``n_days`` steps. Same semantics as
    ``pct_change(n_days)`` but named for its intended use in momentum-style
    features.
    """
    return prices.pct_change(periods=n_days)


def rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index using Wilder's smoothing.

    Implementation equivalent to the canonical formula used by
    ``pandas_ta.rsi`` (EWM with ``alpha = 1/period``, ``adjust=False``),
    without pulling in the extra dependency. The first ``period`` outputs
    are ``NaN`` because Wilder's average needs that many gain/loss
    observations to warm up.
    """
    delta = prices.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


# ---- Hawkes-process-style volatility impulse response ----------------------


def hawkes_process(series: pd.Series, kappa: float) -> pd.Series:
    """Decay-weighted accumulator: ``x_t ← α · x_{t-1} + v_t`` with
    ``α = exp(-κ)``, scaled by ``κ``. Larger ``κ`` ⇒ faster decay.

    Useful as a "volatility pressure" indicator — feed it an intraday
    true-range or a squared-return series and the output highlights regimes
    where large bars cluster.
    """
    if kappa <= 0.0:
        raise ValueError(f"kappa must be positive, got {kappa}")
    alpha = math.exp(-kappa)
    arr = np.asarray(series.to_numpy(), dtype=float)
    out = np.full(len(arr), np.nan, dtype=float)
    for i in range(1, len(arr)):
        prev = out[i - 1]
        out[i] = arr[i] if math.isnan(prev) else prev * alpha + arr[i]
    return pd.Series(out * kappa, index=series.index)


# ---- ordinal patterns + permutation entropy --------------------------------


def ordinal_patterns(arr: np.ndarray, d: int) -> np.ndarray:
    """Lehmer-style encoding of the ordinal pattern in a rolling window of
    length ``d``. Each output is an integer in ``[0, d! - 1]`` or ``NaN``
    for the first ``d - 1`` positions.
    """
    if d < 2:
        raise ValueError("d must be >= 2")
    arr = np.asarray(arr, dtype=float)
    fac = math.factorial(d)
    d1 = d - 1
    mults = [fac / math.factorial(i + 1) for i in range(1, d)]

    ordinals = np.full(len(arr), np.nan, dtype=float)
    for i in range(d1, len(arr)):
        dat = arr[i - d1 : i + 1]
        pattern = 0.0
        for ell in range(1, d):
            count = 0
            for r in range(ell):
                if dat[d1 - ell] >= dat[d1 - r]:
                    count += 1
            pattern += count * mults[ell - 1]
        ordinals[i] = int(pattern)
    return ordinals


def permutation_entropy(
    arr: np.ndarray, d: int = 3, lookback_mult: int = 28
) -> np.ndarray:
    """Rolling-window permutation entropy normalized to ``[0, 1]``.

    Window length is ``d! · lookback_mult``. Low values indicate strong
    temporal structure (monotone sequences, periodic waves); values near 1
    indicate i.i.d.-like randomness.
    """
    fac = math.factorial(d)
    lookback = fac * lookback_mult
    ordinals = ordinal_patterns(arr, d)

    ent = np.full(len(arr), np.nan, dtype=float)
    for i in range(lookback + d - 1, len(arr)):
        window = ordinals[i - lookback + 1 : i + 1]
        counts = pd.Series(window).value_counts(normalize=True)
        pe = -float((counts * np.log2(counts)).sum())
        ent[i] = pe / math.log2(fac)
    return ent


def perm_ts_reversibility(arr: np.ndarray, d: int = 3) -> float:
    """Measure of time-asymmetry via the KL divergence between the ordinal-
    pattern distribution of the forward series and the reversed series.
    Zero for reversible series (sine waves); strictly positive for
    irreversible dynamics (chaotic maps, real markets).

    Uses Laplace smoothing (``+1`` on every pattern count) so the KL stays
    finite on short sequences where some patterns never appear. The
    reference implementation returns ``NaN`` instead; both conventions are
    common in the literature.
    """
    arr = np.asarray(arr, dtype=float)
    if len(arr) < 10:
        raise ValueError("arr must contain at least 10 samples")
    fac = math.factorial(d)

    fwd = ordinal_patterns(arr, d)[d - 1 :].astype(int)
    rev = ordinal_patterns(np.flip(arr), d)[d - 1 :].astype(int)
    n = len(fwd)
    p_f = (np.bincount(fwd, minlength=fac) + 1.0) / (n + fac)
    p_r = (np.bincount(rev, minlength=fac) + 1.0) / (n + fac)
    return float(np.sum(p_f * np.log(p_f / p_r)))


# ---- trendline fitting + breakout signal -----------------------------------


def _check_trend_line(support: bool, pivot: int, slope: float, y: np.ndarray) -> float:
    intercept = -slope * pivot + y[pivot]
    line = slope * np.arange(len(y)) + intercept
    diffs = line - y
    if support and diffs.max() > 1e-5:
        return -1.0
    if not support and diffs.min() < -1e-5:
        return -1.0
    return float((diffs ** 2).sum())


def _optimize_slope(support: bool, pivot: int, init_slope: float, y: np.ndarray) -> tuple[float, float]:
    slope_unit = (y.max() - y.min()) / len(y)
    opt_step = 1.0
    min_step = 0.0001
    curr_step = opt_step

    best_slope = init_slope
    best_err = _check_trend_line(support, pivot, init_slope, y)
    if best_err < 0.0:
        raise ValueError("initial slope is not a valid trend line — check your data")

    get_derivative = True
    derivative: float | None = None
    while curr_step > min_step:
        if get_derivative:
            probe = best_slope + slope_unit * min_step
            err = _check_trend_line(support, pivot, probe, y)
            derivative = err - best_err
            if err < 0.0:
                probe = best_slope - slope_unit * min_step
                err = _check_trend_line(support, pivot, probe, y)
                derivative = best_err - err
            if err < 0.0:
                # Both probe directions are invalid. This happens when the
                # initial polyfit already sits at the constrained optimum
                # (e.g. a perfectly linear series whose pivot lands mid-
                # range); we stop early and return the initial fit rather
                # than the reference's exception.
                break
            get_derivative = False

        step = -1 if (derivative is not None and derivative > 0) else 1
        test_slope = best_slope + step * slope_unit * curr_step
        test_err = _check_trend_line(support, pivot, test_slope, y)
        if test_err < 0 or test_err >= best_err:
            curr_step *= 0.5
        else:
            best_err = test_err
            best_slope = test_slope
            get_derivative = True

    intercept = -best_slope * pivot + y[pivot]
    return best_slope, intercept


def fit_trendlines_single(data: np.ndarray) -> tuple[tuple[float, float], tuple[float, float]]:
    """Fit one support and one resistance line to a closes-only series.
    Returns ``(support, resistance)`` as ``(slope, intercept)`` pairs.
    """
    data = np.asarray(data, dtype=float)
    x = np.arange(len(data))
    coefs = np.polyfit(x, data, 1)
    line = coefs[0] * x + coefs[1]
    upper_pivot = int((data - line).argmax())
    lower_pivot = int((data - line).argmin())
    support = _optimize_slope(True, lower_pivot, coefs[0], data)
    resistance = _optimize_slope(False, upper_pivot, coefs[0], data)
    return support, resistance


def fit_trendlines_high_low(
    high: np.ndarray, low: np.ndarray, close: np.ndarray
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Like ``fit_trendlines_single`` but anchored on the actual highs and
    lows of each bar — more robust when bar-range information is available.
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    x = np.arange(len(close))
    coefs = np.polyfit(x, close, 1)
    line = coefs[0] * x + coefs[1]
    upper_pivot = int((high - line).argmax())
    lower_pivot = int((low - line).argmin())
    support = _optimize_slope(True, lower_pivot, coefs[0], low)
    resistance = _optimize_slope(False, upper_pivot, coefs[0], high)
    return support, resistance


def trendline_breakout_signal(
    close: np.ndarray, lookback: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Rolling-window trendline breakout. For each bar ``i >= lookback``:

    - fit support + resistance on ``close[i-lookback : i]`` (strictly prior)
    - project both lines to bar ``i``
    - signal = +1 if ``close[i] > resistance``, -1 if ``close[i] < support``,
      otherwise carry the previous bar's signal (``ffill``)

    Returns ``(support_proj, resistance_proj, signal)`` each of length N.
    """
    close = np.asarray(close, dtype=float)
    n = len(close)
    s_tl = np.full(n, np.nan, dtype=float)
    r_tl = np.full(n, np.nan, dtype=float)
    sig = np.zeros(n, dtype=float)

    for i in range(lookback, n):
        window = close[i - lookback : i]
        if np.any(np.isnan(window)):
            sig[i] = sig[i - 1]
            continue
        try:
            (s_slope, s_int), (r_slope, r_int) = fit_trendlines_single(window)
        except ValueError:
            sig[i] = sig[i - 1]
            continue
        s_val = s_int + lookback * s_slope
        r_val = r_int + lookback * r_slope
        s_tl[i] = s_val
        r_tl[i] = r_val
        if close[i] > r_val:
            sig[i] = 1.0
        elif close[i] < s_val:
            sig[i] = -1.0
        else:
            sig[i] = sig[i - 1]
    return s_tl, r_tl, sig


# ---- Wald-Wolfowitz runs test ---------------------------------------------


def runs_test_z_score(signs: np.ndarray) -> float:
    """Z-score of the observed number of runs in a ±1 sequence, under the
    null that consecutive signs are independent. A value around 0 is
    consistent with independence; large positive Z ⇒ too many runs
    (over-alternating); large negative Z ⇒ too few runs (clustering).

    Returns ``NaN`` when the sequence is degenerate (all-positive or
    all-negative).
    """
    signs = np.asarray(signs)
    if len(signs) < 2:
        raise ValueError("runs_test requires at least 2 elements")
    n_pos = int(np.sum(signs > 0))
    n_neg = int(np.sum(signs < 0))
    n = n_pos + n_neg
    if n_pos == 0 or n_neg == 0 or n < 2:
        return float("nan")

    expected = 2.0 * n_pos * n_neg / n + 1.0
    variance = (expected - 1.0) * (expected - 2.0) / (n - 1)
    if variance <= 0:
        return float("nan")

    runs = 1
    for i in range(1, len(signs)):
        if signs[i] != signs[i - 1]:
            runs += 1
    return (runs - expected) / math.sqrt(variance)
