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

Labels read the future by design: they are training targets. Any fit
that uses them must keep ``t1`` inside its train window (drop events
whose ``complete`` flag is false) and purge around test blocks
(:mod:`stonks.lab.cv`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd

__all__ = [
    "avg_uniqueness",
    "concurrency",
    "ewma_vol",
    "sequential_bootstrap",
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
