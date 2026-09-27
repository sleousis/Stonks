"""Combinatorial purged cross-validation test (BL-45, López de Prado, *AFML* ch. 12).

Walk-forward gives one backtest path. CPCV gives several: the dataset's
full window is cut into ``n_groups`` contiguous groups of trading days,
and for every combination of ``n_test_groups`` of them (15 splits for 6
and 2) the strategy is re-tuned and fitted on the other groups and
backtested on the held-out ones. The held-out results chain into
``C(N-1, k-1)`` complete paths (5 for 6 and 2), each covering every group
once with a model that never saw it.

Leakage control (principle P9): the training segments of a split are
purged of the ``h`` trading days before each test group and embargoed for
``max(h, ceil(embargo_pct * n))`` days after it, where ``h`` is the
strategy's effective embargo (``max(embargo_bars, label_horizon_bars)``).
A split's dataset carries the segments as ``train_segments``: a strategy
that fits on several windows reads ``train_windows``, one that reads only
``train_window`` gets the longest segment. The tuner's objective scores
``train_window``, so re-tuning also sees only purged training data.

Re-tuning (``retune``): ``"auto"`` re-tunes each split when the lab run
bound a tuning setup (``bind_tuning``) and otherwise only re-fits the
strategy's own params; ``"never"`` always only re-fits. A strategy with
no fit and no re-tune gives identical paths.

Passing requires both

- at least ``min_positive_share`` (0.6) of the path Sharpes above zero, and
- the pooled PSR at least ``min_psr`` (0.9). Every held-out day sits in
  every path, so the paths are pooled into one series of ``n_days`` (the
  bar-by-bar mean across paths) before the PSR (against a zero Sharpe,
  with its skew, kurtosis and autocorrelation). Stacking the paths end to
  end would count each day once per path and shrink the PSR's error
  about ``sqrt(n_paths)`` times (BE-08).

Splits run as ``lab.parallel.run_tasks`` tasks over a lake snapshot, so
the report is the same for any worker count.
"""

from __future__ import annotations

import dataclasses
import math
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.backtesting import run_backtest
from stonks.lab.cv import (
    CombinatorialPurgedKFold,
    purge_horizon,
    refit,
    segments_from_indices,
    trading_dates,
)
from stonks.lab.parallel import (
    DatasetSpec,
    PortableStrategy,
    dataset_snapshot,
    planned_workers,
    run_tasks,
)
from stonks.lab.survival.base import TuningSetup
from stonks.lab.survival.walk_forward import psr0
from stonks.lab.tuning.base import tune_and_fit
from stonks.strategies.base import strategy_data_tickers


@dataclass(frozen=True)
class _Split:
    index: int
    segments: tuple[tuple[date, date], ...]
    #: ``group -> (first day, last day)`` of every test group of the split.
    tests: tuple[tuple[int, date, date], ...]


@dataclass(frozen=True)
class _SplitResult:
    #: ``group -> per-bar returns`` of the split's backtest of that group.
    returns: dict[int, np.ndarray]
    periods_per_year: float


@dataclass(frozen=True)
class _Job:
    dataset: Any
    strategy: PortableStrategy
    setup: TuningSetup | None
    fixed: dict[str, Any]


def _open(job: _Job) -> _Job:
    if isinstance(job.dataset, DatasetSpec):
        return dataclasses.replace(job, dataset=job.dataset.open())
    return job


def _fold_dataset(dataset: Any, segments: tuple[tuple[date, date], ...]) -> Any:
    with_segments = getattr(dataset, "with_train_segments", None)
    if callable(with_segments):
        return with_segments(segments)
    return dataclasses.replace(dataset, train_segments=segments)


def _run_split(job: _Job, split: _Split) -> _SplitResult:
    fold = _fold_dataset(job.dataset, split.segments)
    strategy = job.strategy.strategy
    if job.setup is not None:
        fitted, _ = tune_and_fit(type(strategy), fold, job.setup, job.fixed)
    else:
        fitted = refit(strategy, fold)
    returns: dict[int, np.ndarray] = {}
    ppy = 252.0
    for group, lo, hi in split.tests:
        report = run_backtest(fitted, fold, (lo, hi))
        returns[group] = np.asarray(report.returns, dtype=float)
        ppy = float(report.periods_per_year)
    return _SplitResult(returns=returns, periods_per_year=ppy)


def pooled_psr(path_returns: list[np.ndarray]) -> float:
    """PSR against zero of the paths pooled over their shared days: the
    bar-by-bar mean when the paths line up, else the median path PSR.
    Each day counts once, whatever the number of paths (BE-08)."""
    if not path_returns:
        return math.nan
    if len({len(r) for r in path_returns}) == 1:
        with np.errstate(all="ignore"):
            mean = np.nanmean(np.vstack(path_returns), axis=0)
        return psr0(mean[np.isfinite(mean)])
    return float(np.nanmedian([psr0(r[np.isfinite(r)]) for r in path_returns]))


def _sharpe(returns: np.ndarray, periods_per_year: float) -> float:
    r = returns[np.isfinite(returns)]
    if len(r) < 2:
        return float("nan")
    sd = float(r.std(ddof=1))
    if not sd > 0:
        return float("nan")
    return float(r.mean() / sd * math.sqrt(periods_per_year))


class CPCVTest:
    """Combinatorial purged CV: most backtest paths of models that never
    saw their data must have a positive Sharpe, and the pooled PSR must
    clear 0.9."""

    id = "cpcv"

    class Options(BaseModel):
        model_config = ConfigDict(extra="forbid")

        n_groups: int = Field(
            default=6,
            ge=3,
            le=12,
            description="Blocks of trading days the full window is cut into.",
        )
        n_test_groups: int = Field(
            default=2,
            ge=1,
            le=6,
            description="Blocks held out in each split. Must be fewer than the blocks.",
        )
        embargo_pct: float = Field(
            default=0.01,
            ge=0.0,
            lt=0.5,
            description="Share of days skipped after each held-out block so training never peeks.",
        )
        min_positive_share: float = Field(
            default=0.6,
            ge=0.0,
            le=1.0,
            description="Share of backtest paths that must have a Sharpe above zero.",
        )
        min_psr: float = Field(
            default=0.9,
            gt=0.0,
            lt=1.0,
            description="Lowest chance, over all paths together, that the true Sharpe is above zero.",
        )
        retune: Literal["auto", "never"] = Field(
            default="auto",
            description="auto tunes again on each split when the run tunes. never only refits.",
        )
        #: Worker processes for the splits; ``None`` means the default.
        max_workers: int | None = Field(default=None, ge=1)
        seed: int | None = 0

    def __init__(self, options: CPCVTest.Options | None = None) -> None:
        self.options = options or CPCVTest.Options()
        if self.options.n_test_groups >= self.options.n_groups:
            raise ValueError("n_test_groups must be smaller than n_groups")
        self._setup: TuningSetup | None = None

    @classmethod
    def build(cls, options: CPCVTest.Options) -> CPCVTest:
        return cls(options)

    def bind_tuning(self, setup: TuningSetup) -> None:
        self._setup = setup

    def run(self, strategy: Strategy, context: Any) -> SurvivalReport:
        o = self.options
        cv = CombinatorialPurgedKFold(o.n_groups, o.n_test_groups, o.embargo_pct)
        dates = trading_dates(context, context.full_window)
        if len(dates) < 2 * o.n_groups:
            return SurvivalReport(
                test_id=self.id,
                passed=False,
                metrics={"n_days": float(len(dates))},
                notes=f"insufficient data: {len(dates)} trading days for {o.n_groups} groups",
            )
        horizon = purge_horizon(context, strategy)
        positions = np.arange(len(dates))
        bounds = cv.group_bounds(len(dates))
        splits = [
            _Split(
                index=i,
                segments=tuple(segments_from_indices(dates, s.train)),
                tests=tuple((g, dates[bounds[g][0]], dates[bounds[g][1]]) for g in s.test_groups),
            )
            for i, s in enumerate(cv.split(positions, positions + horizon))
        ]
        results = self._run_splits(strategy, context, splits)
        ppy = results[0].periods_per_year if results else 252.0
        path_returns = [
            np.concatenate([results[split].returns[g] for g, split in enumerate(path)])
            for path in cv.paths()
        ]
        sharpes = [_sharpe(r, ppy) for r in path_returns]
        positive = sum(1 for s in sharpes if s > 0) / len(sharpes)
        path_psrs = [psr0(r[np.isfinite(r)]) for r in path_returns]
        pooled = pooled_psr(path_returns)
        metrics: dict[str, float] = {
            "n_splits": float(cv.n_splits),
            "n_paths": float(cv.n_paths),
            "n_days": float(len(dates)),
            "purge_bars": float(horizon),
            "positive_share": positive,
            "psr0_pooled": pooled,
            "sharpe_mean": float(np.nanmean(sharpes)) if any(np.isfinite(sharpes)) else math.nan,
            "sharpe_min": float(np.nanmin(sharpes)) if any(np.isfinite(sharpes)) else math.nan,
            "retuned": 1.0 if self._retunes() else 0.0,
        }
        for i, s in enumerate(sharpes):
            metrics[f"sharpe_path_{i}"] = s
        for i, p in enumerate(path_psrs):
            metrics[f"psr0_path_{i}"] = p
        failures = []
        if positive < o.min_positive_share:
            failures.append(f"positive paths {positive:.0%} < {o.min_positive_share:.0%}")
        if not pooled >= o.min_psr:
            failures.append(f"pooled PSR {pooled:.3f} < {o.min_psr}")
        return SurvivalReport(
            test_id=self.id,
            passed=not failures,
            metrics=metrics,
            notes="; ".join(failures)
            or f"{cv.n_paths} paths, {positive:.0%} positive, pooled PSR {pooled:.3f}",
        )

    def _retunes(self) -> bool:
        return self.options.retune == "auto" and self._setup is not None

    def _run_splits(
        self, strategy: Strategy, context: Any, splits: list[_Split]
    ) -> list[_SplitResult]:
        setup = self._setup if self._retunes() else None
        job = _Job(
            dataset=context,
            strategy=PortableStrategy(strategy),
            setup=setup,
            fixed=setup.retune_fixed_params(strategy) if setup is not None else {},
        )
        pooled = planned_workers(len(splits), max_workers=self.options.max_workers) > 1
        extra = strategy_data_tickers(strategy)
        with dataset_snapshot(context, extra) if pooled else nullcontext(context) as shipped:
            return run_tasks(
                _run_split,
                splits,
                setup=_open,
                payload=dataclasses.replace(job, dataset=shipped),
                max_workers=self.options.max_workers,
                root_seed=self.options.seed,
            )
