"""Versus-random test (BL-35, after Woodriff via Schwager; principle P6).

A tuner run on random data still finds a best trial. The question is
whether the whole search (the bound tuner, objective and budget) finds
clearly more in the real bars than it finds in noise shaped like them.

- **Noise lakes.** ``k`` times, every bar from the train window start to
  the dataset end is rebuilt from the train window's own bars, drawn with
  a stationary block bootstrap (mean block ``block`` bars; each bar's gap,
  high, low and close relatives stay together, and prices are rebuilt in
  log space as in ``permutation.py``). One draw is shared by the whole
  universe on the union timeline, so cross-asset correlation survives,
  while calendar alignment and any structure longer than a block do not.
  History before the train window stays real (indicator warm-up); the
  validation window is synthetic too and never sees a real validation
  bar. Bars are split- and dividend-adjusted first, and the lakes carry
  no corporate-action rows (as in the MCPT).
- **Scores.** On the real bars and on each noise lake, the same
  ``tune -> fit`` (``TuningSetup``, from ``bind_tuning`` / ``bind_run``)
  gives the tuner's best score; the tuned strategy's backtest over the
  validation window (embargoed for the strategy) gives its OOS score
  (``oos_metric``, Sharpe by default). The real run goes through exactly
  the same lake-building machinery as the noise runs, so both sides are
  measured alike.
- **Verdict.** Passes when the real best score beats the ``quantile``
  (0.95) quantile of the noise best scores (below the ``1 - quantile``
  quantile for a ``minimize`` objective) **and** the real OOS score beats
  the median noise OOS score. ``p_value`` is the +1-smoothed share of
  noise bests at least as good as the real one. A noise run that fails
  scores NaN and is left out, but fewer than ``max(5, k/2)`` usable noise
  runs fail the test for insufficient data (RS-25).

Parallelism: the ``k + 1`` runs are tasks on the lab pool
(``lab.parallel.run_tasks``); a task's inner tuner runs in-process there
(pools never nest). Noise seeds are ``lab.parallel.task_seeds(seed, k)``,
drawn up front, so the report is identical for any ``max_workers``.
Cost: ``(k + 1) * budget`` backtests.
"""

from __future__ import annotations

import dataclasses
import math
import pickle
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from stonks.core.interval import Interval
from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.backtesting import run_backtest
from stonks.lab.dataset import data_tickers, scoring_window
from stonks.lab.parallel import PortableLake, planned_workers, run_tasks, task_seeds
from stonks.lab.survival.base import TuningSetup
from stonks.lab.survival.permutation import (
    _apply_permutation,
    _coarser_intervals,
    _history_bars,
    _modified_lake,
)
from stonks.lab.tuning.base import tune_and_fit
from stonks.logging import get_logger
from stonks.stats.bootstrap import stationary_bootstrap_indices

_log = get_logger("stonks.lab.survival.vs_random")


def bootstrap_noise_bars(
    history: Mapping[str, tuple[pd.DataFrame, int]],
    train_end: date,
    *,
    block: float,
    seed: int,
) -> dict[str, pd.DataFrame]:
    """Noise copies of ``history`` (ticker -> (bars oldest first, number of
    bars before the train window)): every bar from the train window start
    on is rebuilt from bootstrapped train-window bar relatives (see module
    doc). A ticker with no train-window bar keeps only its earlier bars."""
    pools: dict[str, tuple[int, pd.DatetimeIndex]] = {}
    rebuilt: dict[str, pd.DatetimeIndex] = {}
    for ticker, (bars, n_before) in history.items():
        ts = pd.DatetimeIndex(pd.to_datetime(bars["timestamp"]))
        first = max(n_before, 1)  # the first bar has no gap relative
        in_train = np.nonzero((np.arange(len(ts)) >= first) & (ts.date <= train_end))[0]
        if in_train.size:
            pools[ticker] = (first, ts[in_train])
            rebuilt[ticker] = ts[first:]
    out = {t: bars.copy() for t, (bars, _) in history.items()}
    for ticker, (bars, n_before) in history.items():
        if ticker not in pools:
            out[ticker] = bars.iloc[: max(n_before, 1) if len(bars) else 0].copy()
    if not pools:
        return out
    union_pool = _union(p for _, p in pools.values())
    union_rebuilt = _union(rebuilt.values())
    rng = np.random.default_rng(seed)
    draws = math.ceil(len(union_rebuilt) / len(union_pool))
    shared = stationary_bootstrap_indices(
        len(union_pool), min(float(block), len(union_pool)), draws, rng
    ).ravel()[: len(union_rebuilt)]
    for ticker, (first, pool_ts) in pools.items():
        bars, _ = history[ticker]
        slots = union_rebuilt.get_indexer(rebuilt[ticker])
        source_ts = union_pool[shared[slots]]
        # this ticker's pool bar at the drawn stamp (the next one when it
        # has no bar there); its pool rows are contiguous from ``first``,
        # so the pool position is the offset from the first rebuilt row
        rel = np.minimum(pool_ts.searchsorted(source_ts), len(pool_ts) - 1)
        out[ticker] = _apply_permutation(bars, first - 1, rel, rel)
    return out


def _union(indexes: Any) -> pd.DatetimeIndex:
    items = list(indexes)
    out = items[0]
    for ix in items[1:]:
        out = out.union(ix)
    return out.sort_values()


@dataclass(frozen=True)
class _Run:
    """Per-worker state: everything one tune of real or noise bars needs."""

    context: Any  # dataset with its lake detached (``source`` carries it)
    source: PortableLake
    strategy_cls: type[Strategy]
    fixed: dict[str, Any]
    setup: TuningSetup
    interval: Interval
    coarser: list[Interval]
    history: dict[str, tuple[pd.DataFrame, int]]
    train_end: date
    val_window: tuple[date, date]
    block: float
    oos_metric: str


def _score(run: _Run, seed: int | None) -> tuple[float, float]:
    """``(tuner best score, OOS score)`` on the real bars (``seed=None``)
    or on the noise lake drawn from ``seed``; NaNs when the run fails."""
    try:
        if seed is None:
            bars = {t: b for t, (b, _) in run.history.items()}
        else:
            bars = bootstrap_noise_bars(run.history, run.train_end, block=run.block, seed=seed)
        with _modified_lake(
            run.source.lake, data_tickers(run.context), bars, run.interval, run.coarser
        ) as lake:
            dataset = dataclasses.replace(run.context, lake=lake)
            strategy, tuned = tune_and_fit(run.strategy_cls, dataset, run.setup, run.fixed)
            report = run_backtest(strategy, dataset, run.val_window)
            return _best_score(tuned), float(getattr(report, run.oos_metric))
    except Exception as exc:
        _log.warning("vs_random.run_failed", seed=seed, error=str(exc))
        return math.nan, math.nan


def _best_score(tuned: Any) -> float:
    """The tune's best score, NaN when it tried trials and none scored: the
    tuners then report the defaults at 0.0, which is no score."""
    history = list(getattr(tuned, "history", None) or [])
    if history and not any(math.isfinite(float(score)) for _, score in history):
        return math.nan
    return float(tuned.best_score)


class VsRandomOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Noise lakes, each a full tune.
    k: int = Field(
        default=20,
        ge=5,
        le=1000,
        description="How many random look-alike price histories to tune on.",
    )
    #: Mean bootstrap block length, in bars.
    block: float = Field(
        default=20.0, ge=1.0, description="Average length in bars of the chunks used to build them."
    )
    #: Quantile of the noise best scores the real best must beat.
    quantile: float = Field(
        default=0.95,
        ge=0.5,
        lt=1.0,
        description="The real result must beat this share of the random ones.",
    )
    oos_metric: Literal["sharpe", "final_return", "cagr"] = Field(
        default="sharpe", description="Which held-out number to compare."
    )
    seed: int = 17
    #: Worker processes; ``None`` means ``lab.parallel.default_max_workers()``.
    max_workers: int | None = Field(default=None, ge=1)


class VsRandomTest:
    id = "vs_random"
    Options = VsRandomOptions

    def __init__(self, options: VsRandomOptions | None = None, **overrides: Any) -> None:
        base = options or VsRandomOptions()
        self.options = (
            VsRandomOptions.model_validate({**base.model_dump(), **overrides})
            if overrides
            else base
        )
        self._setup: TuningSetup | None = None

    @classmethod
    def build(cls, options: VsRandomOptions) -> VsRandomTest:
        return cls(options)

    def bind_tuning(self, setup: TuningSetup) -> None:
        self._setup = setup

    def bind_run(self, ctx: Any) -> None:
        self.bind_tuning(ctx.setup)

    def run(self, strategy: Strategy, context: Any) -> SurvivalReport:
        opts = self.options
        setup = self._setup
        if setup is None:
            return SurvivalReport(
                self.id,
                False,
                {"k": float(opts.k)},
                "no tuning setup: run under LabRunner or call bind_tuning",
            )
        train_start, train_end = context.train_window
        interval = getattr(context, "interval", Interval.DAY_1)
        history = _history_bars(context, interval, (train_start, context.end))
        if not any(len(b) > n for b, n in history.values()):
            return SurvivalReport(
                self.id, False, {"k": float(opts.k)}, "no bars in the train window; test skipped"
            )
        state = _Run(
            context=dataclasses.replace(context, lake=None),
            source=PortableLake(context.lake, data_tickers(context)),
            strategy_cls=type(strategy),
            fixed=setup.retune_fixed_params(strategy),
            setup=setup,
            interval=interval,
            coarser=_coarser_intervals(context, interval),
            history=history,
            train_end=train_end,
            val_window=scoring_window(context, strategy),
            block=opts.block,
            oos_metric=opts.oos_metric,
        )
        tasks: list[int | None] = [None, *task_seeds(opts.seed, opts.k)]
        workers = planned_workers(len(tasks), max_workers=opts.max_workers)
        if workers > 1:
            problem = _unpicklable(state)
            if problem is not None:
                _log.warning("vs_random.parallel.unavailable", workers=workers, reason=problem)
                workers = 1
        _log.info("vs_random.start", k=opts.k, workers=workers, seed=opts.seed)
        results = run_tasks(_score, tasks, payload=state, max_workers=workers)
        return self._verdict(results, setup.objective, state)

    def _verdict(
        self, results: list[tuple[float, float]], objective: Any, state: _Run
    ) -> SurvivalReport:
        opts = self.options
        (real_best, real_oos), noise = results[0], results[1:]
        bests = np.array([b for b, _ in noise if math.isfinite(b)], dtype=float)
        oos = np.array([o for _, o in noise if math.isfinite(o)], dtype=float)
        minimize = getattr(objective, "direction", "maximize") == "minimize"
        metrics: dict[str, float] = {
            "k": float(opts.k),
            "n_noise_ok": float(bests.size),
            "real_best": real_best,
            "real_oos": real_oos,
        }
        span = f"objective={getattr(objective, 'name', '?')}; oos={opts.oos_metric}"
        # RS-25: a noise quantile from a handful of surviving runs is no bar
        need = max(5, math.ceil(opts.k / 2))
        if bests.size < need or oos.size < need or not math.isfinite(real_best):
            note = f"insufficient data: {bests.size} of {opts.k} noise runs scored ({need} needed)"
            if not math.isfinite(real_best):
                note = "the real tune failed"
            return SurvivalReport(self.id, False, metrics, f"{note}; {span}")
        q = 1.0 - opts.quantile if minimize else opts.quantile
        threshold = float(np.quantile(bests, q))
        oos_median = float(np.median(oos))
        beats = bests <= real_best if minimize else bests >= real_best
        metrics.update(
            {
                "noise_best_q": threshold,
                "noise_best_median": float(np.median(bests)),
                "noise_best_max": float(bests.min() if minimize else bests.max()),
                "noise_oos_median": oos_median,
                "p_value": float((beats.sum() + 1) / (bests.size + 1)),
                "quantile": opts.quantile,
            }
        )
        best_ok = real_best < threshold if minimize else real_best > threshold
        oos_ok = math.isfinite(real_oos) and real_oos > oos_median
        failures = []
        if not best_ok:
            failures.append(
                f"real best {real_best:.4g} does not beat the noise q{opts.quantile:g} "
                f"{threshold:.4g}"
            )
        if not oos_ok:
            failures.append(f"real OOS {real_oos:.4g} <= noise OOS median {oos_median:.4g}")
        head = "; ".join(failures) if failures else "the search beats the same search on noise"
        return SurvivalReport(self.id, not failures, metrics, f"{head}; {span}")


def _unpicklable(obj: Any) -> str | None:
    try:
        pickle.dumps(obj)
    except Exception as exc:  # pickle raises many types
        return f"{type(exc).__name__}: {exc}"
    return None
