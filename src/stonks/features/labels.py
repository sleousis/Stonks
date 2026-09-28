"""Labelling toolkit for ML strategies (BL-45, López de Prado, *AFML* ch. 3-4).

- :func:`ewma_vol`: EWMA standard deviation of log returns (span 100 by
  default), the barrier width unit. Causal: the value at a bar uses only
  that bar and earlier ones.
- :func:`triple_barrier`: for each event, the first of an upper barrier
  (take profit), a lower barrier (stop loss) and a vertical barrier
  (``max_hold`` bars). Widths are ``tp_mult`` / ``sl_mult`` times the
  volatility at the event. With ``high`` / ``low`` an intrabar touch
  counts and fills at the barrier; a bar touching both counts as the stop
  (the conservative reading). The label is 1 (take profit), -1 (stop) or
  0 (time), ``t1`` is when it became known.
- :func:`concurrency`, :func:`avg_uniqueness`: how many labels are open
  on each bar, and each label's mean ``1 / concurrency`` over its span.
  Overlapping labels are not independent samples, so uniqueness is the
  natural sample weight.
- :func:`sequential_bootstrap`: draws events with probability
  proportional to their uniqueness given the draws so far, so a bootstrap
  sample is closer to independent than a plain one.
- :func:`frac_diff_ffd`: fractional differencing with a fixed-width window
  (AFML ch. 5). It keeps memory that a plain return throws away, and
  :func:`min_ffd_d` finds the smallest ``d`` that passes a stationarity
  test (:class:`StationarityTest`, the ADF test by default). Causal.
- :func:`cusum_events`: the symmetric CUSUM filter (AFML 2.5.2.1). An event
  fires when the drift since the last event passes a threshold. Causal.
- :func:`trend_scanning`: labels each event by the sign of the strongest
  linear trend over the next few bars (the largest ``|t|`` of the slope).
  Reads the future by design, like the triple barrier.

Research discipline for the tools that choose something: pick ``d`` on
the training window only and keep it for the test window, and count every
``d`` (or threshold) you tried as a trial. :class:`FracDiffResult` lists
the tries for that reason.

Labels read the future by design: they are training targets. Any fit
that uses them must keep ``t1`` inside its train window (drop events
whose ``complete`` flag is false) and purge around test blocks
(:mod:`stonks.lab.cv`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

__all__ = [
    "ADFTest",
    "FracDiffResult",
    "StationarityResult",
    "StationarityTest",
    "avg_uniqueness",
    "concurrency",
    "cusum_events",
    "ewma_vol",
    "ffd_weights",
    "frac_diff_ffd",
    "min_ffd_d",
    "sequential_bootstrap",
    "trend_scanning",
    "triple_barrier",
]

#: Float slack when comparing a log price with a barrier level.
_EPS = 1e-12

#: Integer bar positions, as a sequence or an array.
IntSeq = Sequence[int] | np.ndarray

_COLUMNS = ["t0_pos", "t1", "t1_pos", "ret", "label", "barrier", "complete", "vol"]


def ewma_vol(close: pd.Series, span: int = 100) -> pd.Series:
    """EWMA standard deviation of ``close``'s log returns (NaN on the
    first bar, which has no return)."""
    if span < 2:
        raise ValueError(f"span must be >= 2, got {span}")
    log_returns = pd.Series(np.log(close.to_numpy(dtype=float)), index=close.index).diff()
    return log_returns.ewm(span=span, min_periods=1).std()


def _positions(index: pd.Index, events: Sequence[Any]) -> list[int]:
    out: list[int] = []
    for event in events:
        if isinstance(event, int | np.integer):
            out.append(int(event))
        else:
            loc = index.get_loc(event)
            if not isinstance(loc, int | np.integer):
                raise KeyError(f"event {event!r} is not a unique bar")
            out.append(int(loc))
    return out


def triple_barrier(
    close: pd.Series,
    events: Sequence[Any],
    *,
    tp_mult: float = 1.0,
    sl_mult: float = 1.0,
    max_hold: int = 10,
    vol: pd.Series | None = None,
    vol_span: int = 100,
    high: pd.Series | None = None,
    low: pd.Series | None = None,
) -> pd.DataFrame:
    """Triple-barrier outcome of each event (see the module doc).

    ``events`` are bar labels of ``close``'s index or integer positions.
    Returns one row per event with a usable volatility, indexed by the
    event's bar: ``t0_pos``, ``t1`` / ``t1_pos`` (the exit bar), ``ret``
    (log return to the exit), ``label``, ``barrier`` (``tp``, ``sl`` or
    ``time``), ``complete`` (false when the data ended before any barrier)
    and ``vol``."""
    if max_hold < 1:
        raise ValueError(f"max_hold must be >= 1, got {max_hold}")
    if not (tp_mult > 0 and sl_mult > 0):
        raise ValueError("tp_mult and sl_mult must be positive")
    log_close = np.log(close.to_numpy(dtype=float))
    log_high = np.log(high.to_numpy(dtype=float)) if high is not None else log_close
    log_low = np.log(low.to_numpy(dtype=float)) if low is not None else log_close
    sigma = (vol if vol is not None else ewma_vol(close, vol_span)).to_numpy(dtype=float)
    n = len(log_close)
    rows: list[dict[str, Any]] = []
    index: list[Any] = []
    for i in _positions(close.index, events):
        width = sigma[i]
        if not (np.isfinite(width) and width > 0):
            continue
        tp = log_close[i] + tp_mult * width
        sl = log_close[i] - sl_mult * width
        last = min(i + max_hold, n - 1)
        exit_pos, barrier, ret = last, "time", log_close[last] - log_close[i]
        for j in range(i + 1, last + 1):
            if log_low[j] <= sl + _EPS:
                exit_pos, barrier, ret = j, "sl", sl - log_close[i]
                break
            if log_high[j] >= tp - _EPS:
                exit_pos, barrier, ret = j, "tp", tp - log_close[i]
                break
        if high is None and barrier != "time":
            ret = log_close[exit_pos] - log_close[i]  # close-only: filled at the close
        rows.append(
            {
                "t0_pos": i,
                "t1": close.index[exit_pos],
                "t1_pos": exit_pos,
                "ret": float(ret),
                "label": {"tp": 1, "sl": -1, "time": 0}[barrier],
                "barrier": barrier,
                "complete": barrier != "time" or i + max_hold <= n - 1,
                "vol": float(width),
            }
        )
        index.append(close.index[i])
    return pd.DataFrame(rows, index=pd.Index(index), columns=_COLUMNS)


def _spans(t0: IntSeq, t1: IntSeq) -> tuple[np.ndarray, np.ndarray]:
    start = np.asarray(t0, dtype=int)
    end = np.asarray(t1, dtype=int)
    if start.shape != end.shape:
        raise ValueError("t0 and t1 must have the same length")
    if np.any(end < start) or np.any(start < 0):
        raise ValueError("every span needs 0 <= t0 <= t1")
    return start, end


def concurrency(t0: IntSeq, t1: IntSeq, n: int | None = None) -> np.ndarray:
    """Number of labels open on each of ``n`` bars (spans are inclusive
    bar positions)."""
    start, end = _spans(t0, t1)
    size = int(n if n is not None else (end.max() + 1 if len(end) else 0))
    delta = np.zeros(size + 1, dtype=int)
    np.add.at(delta, start, 1)
    np.add.at(delta, np.minimum(end + 1, size), -1)
    return np.cumsum(delta)[:size]


def avg_uniqueness(t0: IntSeq, t1: IntSeq) -> np.ndarray:
    """Each label's mean ``1 / concurrency`` over its span, in ``(0, 1]``."""
    start, end = _spans(t0, t1)
    if len(start) == 0:
        return np.empty(0)
    inv = 1.0 / np.maximum(concurrency(start, end), 1)
    cum = np.concatenate([[0.0], np.cumsum(inv)])
    return (cum[end + 1] - cum[start]) / (end - start + 1)


def sequential_bootstrap(
    t0: IntSeq, t1: IntSeq, n_draws: int | None = None, seed: int | None = 0
) -> np.ndarray:
    """Indices of ``n_draws`` events (default: as many as there are) drawn
    one by one, each with probability proportional to its average
    uniqueness against the events already drawn (AFML 4.5.3)."""
    start, end = _spans(t0, t1)
    k = len(start)
    draws = k if n_draws is None else int(n_draws)
    rng = np.random.default_rng(seed)
    size = int(end.max() + 1) if k else 0
    counts = np.zeros(size, dtype=float)
    lengths = end - start + 1
    chosen = np.empty(draws, dtype=int)
    for d in range(draws):
        inv = 1.0 / (counts + 1.0)
        cum = np.concatenate([[0.0], np.cumsum(inv)])
        uniqueness = (cum[end + 1] - cum[start]) / lengths
        pick = int(rng.choice(k, p=uniqueness / uniqueness.sum()))
        chosen[d] = pick
        counts[start[pick] : end[pick] + 1] += 1.0
    return chosen


# ---- fractional differencing (AFML ch. 5) -------------------------------------


def ffd_weights(d: float, threshold: float = 1e-4, width: int | None = None) -> np.ndarray:
    """Weights of the fixed-width fractional difference of order ``d``:
    ``w[0] = 1`` applies to the current bar, ``w[k] = -w[k-1] (d - k + 1) / k``
    to the bar ``k`` back. The window stops before the first weight whose
    size falls below ``threshold``, or at ``width`` weights."""
    if d < 0:
        raise ValueError(f"d must be >= 0, got {d}")
    if not threshold > 0:
        raise ValueError(f"threshold must be positive, got {threshold}")
    if width is not None and width < 1:
        raise ValueError(f"width must be >= 1, got {width}")
    weights = [1.0]
    k = 1
    while width is None or k < width:
        w = -weights[-1] * (d - k + 1) / k
        if abs(w) < threshold:
            break
        weights.append(w)
        k += 1
    return np.asarray(weights, dtype=float)


def frac_diff_ffd(
    series: pd.Series, d: float, threshold: float = 1e-4, width: int | None = None
) -> pd.Series:
    """Fixed-width fractional difference of ``series`` (pass log prices).
    The value at a bar is ``sum_k w[k] x[t-k]`` over the window of
    :func:`ffd_weights`, so it reads that bar and earlier ones only. NaN
    during the warm-up and wherever the window holds a NaN."""
    weights = ffd_weights(d, threshold, width)
    values = series.to_numpy(dtype=float)
    n, span = len(values), len(weights)
    out = np.full(n, np.nan)
    if n >= span:
        # windows[i] is values[i : i + span], whose newest bar is i + span - 1
        windows = np.lib.stride_tricks.sliding_window_view(values, span)
        out[span - 1 :] = windows @ weights[::-1]
    return pd.Series(out, index=series.index, name=series.name)


@dataclass(frozen=True)
class StationarityResult:
    statistic: float
    pvalue: float


class StationarityTest(ABC):
    """A unit-root style test: a small p-value means stationary."""

    @abstractmethod
    def run(self, values: np.ndarray) -> StationarityResult: ...


class ADFTest(StationarityTest):
    """Augmented Dickey-Fuller test (statsmodels ``adfuller``, wrapped here
    so no statsmodels type leaves this module). ``maxlag=1`` and a constant
    by default, as in AFML 5.5."""

    def __init__(self, maxlag: int | None = 1, regression: str = "c") -> None:
        self.maxlag = maxlag
        self.regression = regression

    def run(self, values: np.ndarray) -> StationarityResult:
        from statsmodels.tsa.stattools import adfuller

        clean = np.asarray(values, dtype=float)
        clean = clean[np.isfinite(clean)]
        options: dict[str, Any] = {"autolag": None, "result_object": False}
        out = adfuller(clean, maxlag=self.maxlag, regression=self.regression, **options)
        return StationarityResult(statistic=float(out[0]), pvalue=float(out[1]))


@dataclass(frozen=True)
class FracDiffResult:
    """The smallest ``d`` that passed (``None`` when none did) and every
    ``(d, p-value)`` tried, in order. Each try is a trial."""

    d: float | None
    pvalue: float | None
    tried: list[tuple[float, float]] = field(default_factory=list)

    @property
    def n_tried(self) -> int:
        return len(self.tried)


_D_GRID = tuple(float(v) for v in np.round(np.linspace(0.0, 1.0, 21), 10))


def min_ffd_d(
    series: pd.Series,
    *,
    d_grid: Iterable[float] = _D_GRID,
    threshold: float = 1e-4,
    width: int | None = None,
    pvalue: float = 0.05,
    test: StationarityTest | None = None,
    min_obs: int = 20,
) -> FracDiffResult:
    """Walk ``d_grid`` upwards and return the first ``d`` whose fractional
    difference passes ``test`` at ``pvalue``. Pass the training window
    only: the chosen ``d`` is then fixed for any later data. A ``d`` whose
    window leaves fewer than ``min_obs`` values is not tried."""
    checker = test or ADFTest()
    tried: list[tuple[float, float]] = []
    for d in sorted(float(v) for v in d_grid):
        diffed = frac_diff_ffd(series, d, threshold, width).dropna()
        if len(diffed) < min_obs:
            continue
        result = checker.run(diffed.to_numpy())
        tried.append((d, result.pvalue))
        if result.pvalue < pvalue:
            return FracDiffResult(d=d, pvalue=result.pvalue, tried=tried)
    return FracDiffResult(d=None, pvalue=None, tried=tried)


# ---- CUSUM event filter (AFML 2.5.2.1) ----------------------------------------


def cusum_events(series: pd.Series, threshold: float | pd.Series) -> pd.Index:
    """Bars where the symmetric CUSUM of ``series``'s changes passes the
    threshold (a number, or a series aligned to ``series`` such as a causal
    volatility). Each sum resets after an event. An event at a bar uses
    that bar and earlier ones only."""
    values = series.to_numpy(dtype=float)
    if isinstance(threshold, pd.Series):
        limits = threshold.reindex(series.index).to_numpy(dtype=float)
    else:
        limits = np.full(len(values), float(threshold))
    known = limits[np.isfinite(limits)]
    if np.any(known <= 0) or (not isinstance(threshold, pd.Series) and not threshold > 0):
        raise ValueError("threshold must be positive")
    events: list[int] = []
    up = down = 0.0
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        h = limits[i]
        if not (np.isfinite(change) and np.isfinite(h)):
            continue
        up = max(0.0, up + change)
        down = min(0.0, down + change)
        if up > h + _EPS:
            up = 0.0
            events.append(i)
        elif down < -h - _EPS:
            down = 0.0
            events.append(i)
    return pd.Index(series.index[events])


# ---- trend-scanning labels -----------------------------------------------------

_TREND_COLUMNS = ["t0_pos", "t1", "t1_pos", "tval", "label", "span", "complete"]


def _slope_tvalue(y: np.ndarray) -> float:
    """t-value of the OLS slope of ``y`` on ``0 .. len(y) - 1`` (infinite
    with the slope's sign on a perfect fit)."""
    n = len(y)
    t_c = np.arange(n, dtype=float) - (n - 1) / 2.0
    sxx = float(t_c @ t_c)
    slope = float(t_c @ (y - y.mean())) / sxx
    resid = y - y.mean() - slope * t_c
    s2 = float(resid @ resid) / (n - 2)
    if s2 <= 1e-24:
        return float(np.sign(slope)) * np.inf if slope else 0.0
    return slope / float(np.sqrt(s2 / sxx))


def trend_scanning(
    close: pd.Series,
    events: Sequence[Any] | None = None,
    spans: Iterable[int] = range(5, 21),
) -> pd.DataFrame:
    """Trend-scanning label of each event (every bar by default): among the
    windows of ``spans`` bars starting at the event, the one whose slope has
    the largest ``|t|`` sets the label (its sign) and ``t1`` (its last bar).
    Near the end only the spans that fit are tried and ``complete`` is
    false. Events where no span fits are dropped. Pass log prices."""
    lengths = sorted({int(s) for s in spans})
    if not lengths or lengths[0] < 3:
        raise ValueError("every span must be >= 3 bars")
    values = close.to_numpy(dtype=float)
    n = len(values)
    positions = list(range(n)) if events is None else _positions(close.index, events)
    rows: list[dict[str, Any]] = []
    index: list[Any] = []
    for i in positions:
        best: tuple[float, int] | None = None
        for length in lengths:
            if i + length > n:
                break
            window = values[i : i + length]
            if not np.all(np.isfinite(window)):
                continue
            tval = _slope_tvalue(window)
            if best is None or abs(tval) > abs(best[0]):
                best = (tval, length)
        if best is None:
            continue
        tval, length = best
        end = i + length - 1
        rows.append(
            {
                "t0_pos": i,
                "t1": close.index[end],
                "t1_pos": end,
                "tval": float(tval),
                "label": int(np.sign(tval)),
                "span": length,
                "complete": i + lengths[-1] <= n,
            }
        )
        index.append(close.index[i])
    return pd.DataFrame(rows, index=pd.Index(index), columns=_TREND_COLUMNS)
