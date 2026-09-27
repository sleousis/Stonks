"""Purged cross-validation (BL-45, López de Prado, *AFML* chapters 7 and 12).

Plain k-fold leaks on market data: a label that looks ``h`` bars ahead
reads prices inside the next fold, and serially correlated features carry
information across a fold boundary. Two fixes, applied to every train set:

- **purge**: drop every train sample whose label span ``[t0, t1]``
  overlaps the span of the test block's labels;
- **embargo**: also drop the ``embargo`` samples right after each test
  block (``ceil(embargo_pct * n)``).

Samples are rows sorted by their start time ``t0``; ``t1`` is when each
label is known (``t0`` itself when labels are instant). Both may be
numbers (bar positions) or ``datetime64`` values.

- :class:`PurgedKFold` gives ``n_splits`` contiguous test folds.
- :class:`CombinatorialPurgedKFold` cuts the samples into ``n_groups``
  contiguous groups and tests on every combination of ``n_test_groups``
  of them: ``C(N, k)`` splits (15 for 6 and 2). Each group is tested in
  ``C(N-1, k-1)`` splits, so the test results chain into that many
  complete backtest paths (5 for 6 and 2), each covering every group once.
  :meth:`~CombinatorialPurgedKFold.paths` says which split backs each
  group of each path.
- :class:`CVObjective` wraps a backtest objective: it scores a strategy on
  the purged folds of the train window (each fold fitted on the other
  folds, through ``LabDataset.train_segments``) and averages the scores,
  so a tuner stops selecting on in-sample fit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from itertools import combinations
from typing import Any, Literal, cast

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.protocols import Strategy, TrialOutcome
from stonks.core.timeutil import day_end, day_start

__all__ = [
    "CPCVSplit",
    "CVObjective",
    "CombinatorialPurgedKFold",
    "PurgedKFold",
    "contiguous_runs",
    "purge_days",
    "purge_horizon",
    "purged_train_mask",
    "trading_dates",
]


def _as_numeric(values: Any) -> np.ndarray:
    arr = np.asarray(values)
    if np.issubdtype(arr.dtype, np.datetime64):
        return arr.astype("datetime64[ns]").astype(np.int64)
    return arr.astype(float)


def _prepare(t0: Any, t1: Any | None) -> tuple[np.ndarray, np.ndarray]:
    start = _as_numeric(t0)
    end = start.copy() if t1 is None else _as_numeric(t1)
    if start.ndim != 1 or end.shape != start.shape:
        raise ValueError("t0 and t1 must be one-dimensional and of equal length")
    if np.any(end < start):
        raise ValueError("every label must end on or after it starts (t1 >= t0)")
    if np.any(np.diff(start) < 0):
        raise ValueError("samples must be sorted by t0")
    return start, end


def purged_train_mask(t0: Any, t1: Any, test: np.ndarray, embargo: int = 0) -> np.ndarray:
    """Boolean train mask: every sample not in ``test`` (a boolean mask),
    minus the samples whose label span overlaps a test block's label span
    (purge) and the ``embargo`` samples after each test block."""
    start, end = _prepare(t0, t1)
    test = np.asarray(test, dtype=bool)
    if test.shape != start.shape:
        raise ValueError("test mask must match the samples")
    if embargo < 0:
        raise ValueError(f"embargo must be >= 0, got {embargo}")
    train = ~test
    n = len(start)
    for lo, hi in contiguous_runs(np.flatnonzero(test)):
        block_start = start[lo]
        block_end = end[lo : hi + 1].max()
        overlaps = (start <= block_end) & (end >= block_start)
        train &= ~overlaps
        train[hi + 1 : min(n, hi + 1 + embargo)] = False
    return train


def contiguous_runs(indices: np.ndarray) -> list[tuple[int, int]]:
    """``(first, last)`` of every run of consecutive integers in the sorted
    ``indices``."""
    idx = np.asarray(indices, dtype=int)
    if len(idx) == 0:
        return []
    breaks = np.flatnonzero(np.diff(idx) != 1)
    starts = np.concatenate([[0], breaks + 1])
    ends = np.concatenate([breaks, [len(idx) - 1]])
    return [(int(idx[a]), int(idx[b])) for a, b in zip(starts, ends, strict=True)]


def _embargo_size(n: int, embargo_pct: float) -> int:
    return math.ceil(round(n * embargo_pct, 9))


def _check_pct(embargo_pct: float) -> float:
    if not 0.0 <= embargo_pct < 0.5:
        raise ValueError(f"embargo_pct must lie in [0, 0.5), got {embargo_pct}")
    return float(embargo_pct)


def _bounds(n: int, parts: int) -> list[tuple[int, int]]:
    edges = np.array_split(np.arange(n), parts)
    return [(int(e[0]), int(e[-1])) for e in edges]


class PurgedKFold:
    """k-fold over time-ordered samples with purging and an embargo."""

    def __init__(self, n_splits: int = 5, embargo_pct: float = 0.01) -> None:
        if n_splits < 2:
            raise ValueError(f"n_splits must be >= 2, got {n_splits}")
        self.n_splits = int(n_splits)
        self.embargo_pct = _check_pct(embargo_pct)

    def split(self, t0: Any, t1: Any | None = None) -> list[tuple[np.ndarray, np.ndarray]]:
        """``(train indices, test indices)`` per fold, in fold order."""
        start, end = _prepare(t0, t1)
        n = len(start)
        if n < self.n_splits:
            raise ValueError(f"need at least {self.n_splits} samples, got {n}")
        embargo = _embargo_size(n, self.embargo_pct)
        out = []
        for lo, hi in _bounds(n, self.n_splits):
            test = np.zeros(n, dtype=bool)
            test[lo : hi + 1] = True
            train = purged_train_mask(start, end, test, embargo)
            out.append((np.flatnonzero(train), np.flatnonzero(test)))
        return out


@dataclass(frozen=True)
class CPCVSplit:
    """One combinatorial split: its train and test sample indices and the
    groups it tests on (sorted)."""

    train: np.ndarray
    test: np.ndarray
    test_groups: tuple[int, ...]


class CombinatorialPurgedKFold:
    """Combinatorial purged k-fold (CPCV), see the module doc."""

    def __init__(
        self, n_groups: int = 6, n_test_groups: int = 2, embargo_pct: float = 0.01
    ) -> None:
        if not 1 <= n_test_groups < n_groups:
            raise ValueError(
                f"need 1 <= n_test_groups < n_groups, got {n_test_groups} and {n_groups}"
            )
        self.n_groups = int(n_groups)
        self.n_test_groups = int(n_test_groups)
        self.embargo_pct = _check_pct(embargo_pct)

    @property
    def n_splits(self) -> int:
        return math.comb(self.n_groups, self.n_test_groups)

    @property
    def n_paths(self) -> int:
        return math.comb(self.n_groups - 1, self.n_test_groups - 1)

    def combinations(self) -> list[tuple[int, ...]]:
        """The test groups of every split, in split order."""
        return list(combinations(range(self.n_groups), self.n_test_groups))

    def group_bounds(self, n: int) -> list[tuple[int, int]]:
        """``(first, last)`` sample index of each group over ``n`` samples."""
        if n < self.n_groups:
            raise ValueError(f"need at least {self.n_groups} samples, got {n}")
        return _bounds(n, self.n_groups)

    def split(self, t0: Any, t1: Any | None = None) -> list[CPCVSplit]:
        start, end = _prepare(t0, t1)
        n = len(start)
        bounds = self.group_bounds(n)
        embargo = _embargo_size(n, self.embargo_pct)
        out = []
        for groups in self.combinations():
            test = np.zeros(n, dtype=bool)
            for g in groups:
                lo, hi = bounds[g]
                test[lo : hi + 1] = True
            train = purged_train_mask(start, end, test, embargo)
            out.append(CPCVSplit(np.flatnonzero(train), np.flatnonzero(test), groups))
        return out

    def paths(self) -> list[list[int]]:
        """For each path, the split index whose test result fills each
        group: path ``p`` takes, for group ``g``, the ``p``-th split (in
        split order) that tests ``g``."""
        combos = self.combinations()
        by_group: list[list[int]] = [[] for _ in range(self.n_groups)]
        for index, groups in enumerate(combos):
            for g in groups:
                by_group[g].append(index)
        return [[by_group[g][p] for g in range(self.n_groups)] for p in range(self.n_paths)]


def trading_dates(dataset: Any, window: tuple[date, date]) -> list[date]:
    """The distinct days with a bar of the dataset's universe and interval
    inside ``window``, oldest first."""
    interval = getattr(dataset, "interval", Interval.DAY_1)
    frame = dataset.lake.sql(
        """
        SELECT DISTINCT CAST(timestamp AS DATE) AS day
          FROM bars
         WHERE ticker = ANY(?) AND interval = ? AND timestamp BETWEEN ? AND ?
         ORDER BY day
        """,
        [list(dataset.universe), interval.code, day_start(window[0]), day_end(window[1])],
    )
    return [_to_date(d) for d in frame["day"]]


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return cast(date, pd.Timestamp(value).date())


def segments_from_indices(dates: list[date], indices: np.ndarray) -> list[tuple[date, date]]:
    """The date windows of every run of consecutive ``indices`` into ``dates``."""
    return [(dates[a], dates[b]) for a, b in contiguous_runs(indices)]


def refit(strategy: Strategy, dataset: Any) -> Strategy:
    """A fresh copy of ``strategy`` (same class and params) fitted on
    ``dataset``; the strategy itself when its class learns nothing."""
    from stonks.lab.survival.permutation import has_nontrivial_fit

    if not has_nontrivial_fit(strategy):
        return strategy
    fresh = type(strategy)(dict(getattr(strategy, "params", {}) or {}))
    fresh.fit(dataset)
    return fresh


class CVObjective:
    """``inner`` (a backtest objective with ``metric(report)``) averaged
    over ``folds`` purged folds of the train window (see the module doc).

    Each fold is fitted on the other folds' purged segments and backtested
    on its own dates, with bars before it visible for look-backs as in any
    backtest. The purge horizon is the strategy's effective embargo
    (``max(embargo_bars, label_horizon_bars)``)."""

    def __init__(self, inner: Any, folds: int = 5, embargo_pct: float = 0.01) -> None:
        if folds < 2:
            raise ValueError(f"folds must be >= 2, got {folds}")
        self.inner = inner
        self.folds = int(folds)
        self.embargo_pct = _check_pct(embargo_pct)
        self.name = f"cv_{inner.name}"
        self.direction: Literal["maximize", "minimize"] = getattr(inner, "direction", "maximize")

    def evaluate(self, strategy: Strategy, dataset: Any) -> TrialOutcome:
        from stonks.lab.backtesting import run_backtest
        from stonks.lab.objectives import per_bar_returns

        dates = trading_dates(dataset, dataset.train_window)
        horizon = purge_days(dataset, strategy)
        positions = np.arange(len(dates))
        splits = PurgedKFold(self.folds, self.embargo_pct).split(positions, positions + horizon)
        scores: list[float] = []
        returns: list[np.ndarray] = []
        index: list[np.ndarray] = []
        for train, test in splits:
            segments = segments_from_indices(dates, train)
            if not segments:
                continue
            fitted = refit(strategy, dataset.with_train_segments(segments))
            report = run_backtest(fitted, dataset, (dates[test[0]], dates[test[-1]]))
            scores.append(float(self.inner.metric(report)))
            fold_returns, fold_index = per_bar_returns(report)
            returns.append(fold_returns)
            index.append(fold_index)
        finite = [s for s in scores if math.isfinite(s)]
        return TrialOutcome(
            params=dict(getattr(strategy, "params", None) or {}),
            score=float(np.mean(finite)) if finite else float("nan"),
            returns=np.concatenate(returns) if returns else np.empty(0),
            index=np.concatenate(index) if index else np.empty(0, dtype="datetime64[ns]"),
        )

    def score(self, strategy: Strategy, dataset: Any) -> float:
        return self.evaluate(strategy, dataset).score


def purge_days(dataset: Any, strategy: Any) -> int:
    """:func:`purge_horizon` in the units the folds use: trading days. An
    intraday horizon counts the sessions its bars fill (on the 6.5-hour
    exchange calendar, the longest reading for 24-hour markets), so an
    hourly run no longer purges one day per bar (BE-60)."""
    from stonks.backtest.calendar import EXCHANGE_SESSIONS

    bars = purge_horizon(dataset, strategy)
    interval = getattr(dataset, "interval", Interval.DAY_1)
    if bars <= 0 or not interval.is_intraday:
        return bars
    per_session = EXCHANGE_SESSIONS.periods_per_year(interval) / EXCHANGE_SESSIONS.sessions_per_year
    return math.ceil(round(bars / per_session, 9))


def purge_horizon(dataset: Any, strategy: Any) -> int:
    """Bars to purge around a test block: the dataset's effective embargo
    for ``strategy`` (``max(embargo_bars, label_horizon_bars)``), else the
    strategy's label horizon."""
    effective = getattr(dataset, "effective_embargo_bars", None)
    value: Any = (
        effective(strategy) if effective is not None else getattr(strategy, "label_horizon_bars", 0)
    )
    return int(value or 0)
