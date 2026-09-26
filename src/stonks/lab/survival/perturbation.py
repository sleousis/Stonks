"""Perturbation test: run a baseline backtest, then repeat against copies of
the universe's bars with multiplicative gaussian noise and check how
correlated the equity curves' per-bar returns stay.

The gate is the correlation of per-bar returns (RS-10), not of equity
levels: any two rising curves correlate above 0.8 in level even when the
noisy run makes entirely different trades. The level correlation is still
reported as ``level_correlation_min``. A pair of runs with no return
variance (neither trades) correlates at 0 and fails.

Like the MCPT, each noise level gets its own in-memory ``DuckDBLake``
holding a perturbed copy of the ``bars`` table for the universe (every
interval, full history so strategy look-backs still have data) plus an
unmodified copy of every other table a strategy may read (instruments,
statements, dividends, macro series, …; see ``lab.lake_copy``). Because
the noise is baked into the table once, every read path sees it:
``get_bars``, ``get_prices`` and the engine's own SQL all read the same
perturbed bars, and the same bar always reads back the same perturbed
price. Only prices are noised; the non-bar tables are exact copies.

Noise model: one standard-normal draw ``z`` per bar row (fixed by
``seed``), shared across noise levels; at level ``sigma`` the bar's
``open/high/low/close/adj_close`` are all scaled by ``exp(sigma * z)``,
so OHLC ordering is preserved and prices stay positive. Volume is left
untouched.

Window (BL-21): the baseline and every noisy run backtest the validation
window (``window="val"``, the default: the embargoed window the tuner
never saw, see ``lab.dataset.scoring_window``); ``window="full"`` scores
the whole dataset as before. Noise covers every bar up to the window end,
look-back history included.

Noise levels run as ``lab.parallel.run_tasks`` tasks on ``max_workers``
processes (default ``lab.parallel.default_max_workers()``; 1 runs them
in-process). The noise draws are made once in this process, and each
worker rebuilds the same perturbed lake from them, so the report is
identical for any worker count provided the strategy's ``save``/``load``
round-trip is exact.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.backtesting import run_backtest
from stonks.lab.dataset import LabDataset, ScoringWindow, data_tickers, scoring_window
from stonks.lab.lake_copy import copy_universe_lake
from stonks.lab.parallel import PortableLake, PortableStrategy, run_tasks
from stonks.logging import get_logger
from stonks.store.lake import DuckDBLake

_log = get_logger("stonks.lab.survival.perturbation")

_PRICE_COLS = ("open", "high", "low", "close", "adj_close")


class PerturbationTest:
    id = "perturbation"

    #: Plain words for each option, shown by the console's options editor.
    option_help: ClassVar[dict[str, str]] = {
        "noise_sigmas": "Noise levels added to prices, as fractions.",
        "min_correlation": "Lowest correlation of daily returns with the clean run that passes.",
        "window": "Which data to test on: val is the held-out window, full is all of it.",
    }

    def __init__(
        self,
        noise_sigmas: Sequence[float] = (0.0, 0.005, 0.01),
        min_correlation: float = 0.8,
        seed: int = 0,
        window: ScoringWindow = "val",
        max_workers: int | None = None,
    ) -> None:
        if window not in ("val", "full"):
            raise ValueError(f"window must be 'val' or 'full', got {window!r}")
        if max_workers is not None and max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self._sigmas = list(noise_sigmas)
        self._min_corr = min_correlation
        self._seed = seed
        self._window: ScoringWindow = window
        self._max_workers = max_workers

    def run(self, strategy: Strategy, context: LabDataset) -> SurvivalReport:
        window = scoring_window(context, strategy, self._window)
        _log.info(
            "perturbation.start",
            seed=self._seed,
            sigmas=self._sigmas,
            window=[str(w) for w in window],
        )
        baseline = list(run_backtest(strategy, context, window).equity_curve)

        bars = _universe_bars(context, window[1])
        z = np.random.default_rng(self._seed).standard_normal(len(bars))
        noisy = [s for s in self._sigmas if s != 0.0]
        curves = (
            run_tasks(
                _noisy_curve,
                noisy,
                payload=_NoiseRun(
                    context=dataclasses.replace(context, lake=None),
                    source=PortableLake(context.lake, data_tickers(context)),
                    strategy=PortableStrategy(strategy),
                    bars=bars,
                    z=z,
                    window=window,
                ),
                max_workers=self._max_workers,
            )
            if noisy
            else []
        )
        by_sigma = dict(zip(noisy, curves, strict=True))
        base_returns = _returns(baseline)
        correlations = [
            1.0 if sigma == 0.0 else _pearson(base_returns, _returns(by_sigma[sigma]))
            for sigma in self._sigmas
        ]
        levels = [
            1.0 if sigma == 0.0 else _pearson(baseline, by_sigma[sigma]) for sigma in self._sigmas
        ]

        mean_corr = sum(correlations) / len(correlations) if correlations else 0.0
        min_corr = min(correlations) if correlations else 0.0
        metrics = {
            "correlation_mean": mean_corr,
            "correlation_min": min_corr,
            "level_correlation_min": min(levels) if levels else 0.0,
            "levels_tested": float(len(self._sigmas)),
        }
        passed = min_corr >= self._min_corr
        return SurvivalReport(
            test_id=self.id,
            passed=passed,
            metrics=metrics,
            notes=f"seed={self._seed}; window={self._window}",
        )


@dataclass
class _NoiseRun:
    """Per-worker state of the noisy runs: the dataset (lake detached),
    its universe tables, the strategy, the real bars and the noise draws."""

    context: Any
    source: PortableLake
    strategy: PortableStrategy
    bars: pd.DataFrame
    z: np.ndarray
    window: tuple[date, date]


def _noisy_curve(run: _NoiseRun, sigma: float) -> list[float]:
    lake = _perturbed_lake(run.source.lake, data_tickers(run.context), run.bars, run.z, sigma)
    try:
        report = run_backtest(run.strategy.strategy, run.context, run.window, lake=lake)
    finally:
        lake.close()
    return list(report.equity_curve)


def _universe_bars(context: LabDataset, end: date) -> pd.DataFrame:
    """Every bar (all intervals, all history up to ``end``) for the
    universe and the tickers the strategy reads (RS-01), in a
    deterministic row order so noise draws are stable."""
    return context.lake.sql(
        """
        SELECT ticker, timestamp, interval, open, high, low, close, adj_close, volume
          FROM bars
         WHERE ticker = ANY(?) AND CAST(timestamp AS DATE) <= ?
         ORDER BY ticker, interval, timestamp
        """,
        [data_tickers(context), end],
    )


def _perturbed_lake(
    source: DuckDBLake, universe: Sequence[str], bars: pd.DataFrame, z: np.ndarray, sigma: float
) -> DuckDBLake:
    lake = copy_universe_lake(source, universe)
    if bars.empty:
        return lake
    noisy = bars.copy()
    factor = np.exp(sigma * z)
    for col in _PRICE_COLS:
        noisy[col] = noisy[col].astype(float) * factor
    for code, frame in noisy.groupby("interval", sort=False):
        lake.upsert_bars(frame, interval=Interval.parse(code))
    return lake


def _returns(curve: Sequence[float]) -> list[float]:
    """Per-bar simple returns of an equity curve (0 where the prior mark is
    not positive)."""
    return [(b / a - 1.0) if a > 0 else 0.0 for a, b in zip(curve[:-1], curve[1:], strict=True)]


def _pearson(a: Sequence[float], b: Sequence[float]) -> float:
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    a, b = list(a[:n]), list(b[:n])
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    denom = math.sqrt(va * vb)
    return num / denom if denom > 0 else 0.0
