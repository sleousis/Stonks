"""A vectorised pre-screen for the tuner (BL-49, Hilpisch).

Scoring a parameter set with the event engine costs a full backtest. For a
strategy that can say its target positions for a whole price table at once
(:class:`VectorizedStrategy`), :func:`vectorized_backtest` scores one set in
a few array operations: weights decided at bar ``t``'s close earn the
close-to-close return of bar ``t + 1``, less a flat cost per unit turnover.
That is an approximation (no fills at the open, no participation cap, no
cash rounding), good enough to rank many sets cheaply.

:class:`PrescreenTuner` uses it before the full tuner: it scores a large
grid with the approximation, keeps the best few, and runs the full event
backtest (the objective) only on those. The pre-screen is opt-in and its
scores never stand in for a backtest: the winner is chosen on full scores
only. Screened-out sets still count as trials (principle P2): they appear
in the result's history with a NaN score and ``status="failed"`` (error
``screened_out``), so the trial ledger and the deflated Sharpe see every
configuration that was tried.

A strategy without ``target_positions`` is tuned by the plain grid tuner.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.params import Params, ParamSpace
from stonks.core.protocols import Objective, Strategy, TrialOutcome, TunerResult
from stonks.core.timeutil import as_datetime, day_end
from stonks.lab.parallel import ParallelSettings
from stonks.lab.tuning.base import (
    best_of,
    evaluate_candidates,
    expand_grid,
    grid_axes,
    grid_size_of,
    merge_with_defaults,
    sample_grid,
)
from stonks.lab.tuning.grid import GridTuner
from stonks.logging import get_logger

_log = get_logger("stonks.lab.vectorized")

__all__ = [
    "PrescreenTuner",
    "ScreenedCandidate",
    "VectorizedResult",
    "VectorizedStrategy",
    "load_closes",
    "prescreen",
    "vectorized_backtest",
]

SCREENED_OUT = "screened_out"


@runtime_checkable
class VectorizedStrategy(Protocol):
    """A strategy class that can give its target weights for a whole table.

    ``closes`` holds daily (adjusted) closes, one column per ticker, oldest
    row first. The result has the same shape: the weight of each ticker
    decided at each bar's close (fractions of equity, 0 = flat, NaN = 0).
    Row ``t`` may only use rows ``<= t``."""

    @classmethod
    def target_positions(cls, closes: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame: ...


def supports_vectorized(strategy_cls: type[Any]) -> bool:
    return callable(getattr(strategy_cls, "target_positions", None))


@dataclass(frozen=True)
class VectorizedResult:
    """Per-bar returns of the approximate backtest and their summary."""

    returns: pd.Series
    turnover: float

    @property
    def total_return(self) -> float:
        return float(np.prod(1.0 + self.returns.to_numpy()) - 1.0)

    def sharpe(self, periods_per_year: float = 252.0) -> float:
        values = self.returns.to_numpy()
        if len(values) < 2:
            return float("nan")
        sd = float(np.std(values, ddof=1))
        if sd == 0.0 or not math.isfinite(sd):
            return float("nan")
        return float(np.mean(values)) / sd * math.sqrt(periods_per_year)


def vectorized_backtest(
    closes: pd.DataFrame,
    weights: pd.DataFrame,
    *,
    cost_bps: float = 0.0,
    start: Any = None,
) -> VectorizedResult:
    """Returns of holding ``weights`` (decided at each close) over the next
    bar, less ``cost_bps`` per unit of turnover. Only returns of bars
    stamped on or after ``start`` count (earlier rows are history)."""
    if cost_bps < 0:
        raise ValueError(f"cost_bps must be >= 0, got {cost_bps}")
    w = weights.reindex(index=closes.index, columns=closes.columns).fillna(0.0)
    asset = closes.pct_change(fill_method=None).fillna(0.0)
    held = w.shift(1).fillna(0.0)  # decided at t, earns bar t + 1
    trades = w.diff().abs()
    if len(trades):
        trades.iloc[0] = w.iloc[0].abs()
    gross = (held * asset).sum(axis=1)
    cost = trades.shift(1).fillna(0.0).sum(axis=1) * cost_bps / 10_000.0
    returns = gross - cost
    if start is not None:
        mask = returns.index >= pd.Timestamp(as_datetime(start))
        returns, trades = returns[mask], trades[mask]
    turnover = float(trades.to_numpy().sum()) if len(trades) else 0.0
    return VectorizedResult(returns=returns.astype(float), turnover=turnover)


def load_closes(
    lake: Any,
    tickers: Sequence[str],
    end: Any,
    *,
    interval: Interval = Interval.DAY_1,
) -> pd.DataFrame:
    """Closes (``adj_close`` when present) per ticker, bars stamped on or
    before ``end``, one column per ticker on the union of their stamps."""
    columns: dict[str, pd.Series] = {}
    stop = day_end(end) if not isinstance(end, datetime) else end
    for ticker in tickers:
        bars = lake.get_bars(ticker, interval, datetime(1900, 1, 1), stop)
        if bars is None or bars.empty:
            continue
        col = "adj_close" if "adj_close" in bars.columns else "close"
        values = np.asarray(pd.to_numeric(bars[col], errors="coerce"), dtype=float)
        if col == "adj_close":
            raw = np.asarray(pd.to_numeric(bars["close"], errors="coerce"), dtype=float)
            values = np.where(np.isnan(values), raw, values)
        columns[ticker] = pd.Series(values, index=pd.to_datetime(bars["timestamp"]))
    if not columns:
        return pd.DataFrame()
    return pd.DataFrame(columns).sort_index()


@dataclass(frozen=True)
class ScreenedCandidate:
    params: Params
    score: float


def prescreen(
    strategy_cls: type[Any],
    candidates: Sequence[Params],
    closes: pd.DataFrame,
    *,
    start: Any = None,
    cost_bps: float = 0.0,
    metric: str = "sharpe",
) -> list[ScreenedCandidate]:
    """The approximate score of each candidate, in candidate order. A
    candidate whose weights raise scores NaN."""
    if not supports_vectorized(strategy_cls):
        raise TypeError(f"{strategy_cls.__name__} has no target_positions")
    if metric not in ("sharpe", "total_return"):
        raise ValueError(f"metric must be 'sharpe' or 'total_return', got {metric!r}")
    out: list[ScreenedCandidate] = []
    for params in candidates:
        try:
            weights = strategy_cls.target_positions(closes, params)
            result = vectorized_backtest(closes, weights, cost_bps=cost_bps, start=start)
            score = result.sharpe() if metric == "sharpe" else result.total_return
        except Exception as exc:
            _log.warning("prescreen.candidate_failed", params=dict(params), error=str(exc))
            score = float("nan")
        out.append(ScreenedCandidate(params=dict(params), score=score))
    return out


class PrescreenTuner:
    """Screen a large grid with the vectorised backtest, then run the full
    backtest (the objective) on the best ``budget`` of them (module doc).

    ``grid_size`` points per axis, at most ``max_candidates`` points in all
    (a seeded sample beyond that). ``cost_bps`` charges the screen a flat
    cost per unit turnover, so it does not favour churn."""

    def __init__(
        self,
        grid_size: int = 10,
        max_candidates: int = 2_000,
        *,
        seed: int = 0,
        cost_bps: float = 5.0,
        parallel: ParallelSettings | None = None,
    ) -> None:
        if grid_size < 1 or max_candidates < 1:
            raise ValueError("grid_size and max_candidates must be >= 1")
        self._grid_size = grid_size
        self._max_candidates = max_candidates
        self._seed = seed
        self._cost_bps = cost_bps
        self._parallel = parallel or ParallelSettings()
        #: The last run's screen, in candidate order (for reports).
        self.last_screen: list[ScreenedCandidate] = []

    def tune(
        self,
        strategy_cls: type[Strategy],
        param_space: ParamSpace,
        objective: Objective,
        dataset: Any,
        budget: int,
    ) -> TunerResult:
        if not supports_vectorized(strategy_cls):
            _log.info("prescreen.unsupported", strategy=strategy_cls.__name__)
            return GridTuner(self._grid_size, self._seed, self._parallel).tune(
                strategy_cls, param_space, objective, dataset, budget
            )
        axes = grid_axes(param_space, self._grid_size)
        if grid_size_of(axes) > self._max_candidates:
            points = sample_grid(axes, self._max_candidates, random.Random(self._seed))
        else:
            points = list(expand_grid(param_space, self._grid_size))
        candidates = [merge_with_defaults(p, param_space) for p in points]
        train_start, train_end = dataset.train_window
        closes = load_closes(dataset.lake, list(dataset.universe), train_end)
        self.last_screen = prescreen(
            strategy_cls, candidates, closes, start=train_start, cost_bps=self._cost_bps
        )
        # The screen score is a Sharpe, higher is better, whatever the
        # objective's direction (BE-58): the objective only picks among the
        # full backtests below.
        order = sorted(
            range(len(candidates)),
            key=lambda i: (
                math.isnan(self.last_screen[i].score),
                -self.last_screen[i].score if not math.isnan(self.last_screen[i].score) else 0.0,
                i,
            ),
        )
        kept = sorted(order[: max(1, budget)])
        _log.info(
            "prescreen.done",
            strategy=strategy_cls.__name__,
            screened=len(candidates),
            kept=len(kept),
        )
        full = evaluate_candidates(
            strategy_cls,
            [candidates[i] for i in kept],
            objective,
            dataset,
            parallel=self._parallel,
            root_seed=self._seed,
            log_prefix="prescreen",
        )
        by_index = dict(zip(kept, full, strict=True))
        trials: list[TrialOutcome] = [
            by_index.get(i)
            or TrialOutcome.failed(
                candidates[i], f"{SCREENED_OUT} (pre-screen score {self.last_screen[i].score:.4g})"
            )
            for i in range(len(candidates))
        ]
        best_params, best_score = best_of(full, objective.direction, param_space)
        return TunerResult(
            best_params=best_params,
            best_score=best_score,
            history=[(t.params, t.score) for t in trials],
            trials=trials,
        )
