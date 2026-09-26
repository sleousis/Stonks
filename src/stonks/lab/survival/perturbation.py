"""Perturbation test: run a baseline backtest, then repeat against copies of
the universe's bars with multiplicative gaussian noise and check how
correlated the equity curves stay.

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
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.backtesting import run_backtest
from stonks.lab.dataset import LabDataset
from stonks.lab.lake_copy import copy_universe_lake
from stonks.logging import get_logger
from stonks.store.lake import DuckDBLake

_log = get_logger("stonks.lab.survival.perturbation")

_PRICE_COLS = ("open", "high", "low", "close", "adj_close")


class PerturbationTest:
    id = "perturbation"

    def __init__(
        self,
        noise_sigmas: Sequence[float] = (0.0, 0.005, 0.01),
        min_correlation: float = 0.8,
        seed: int = 0,
    ) -> None:
        self._sigmas = list(noise_sigmas)
        self._min_corr = min_correlation
        self._seed = seed

    def run(self, strategy: Strategy, context: LabDataset) -> SurvivalReport:
        _log.info("perturbation.start", seed=self._seed, sigmas=self._sigmas)
        baseline = list(run_backtest(strategy, context, context.full_window).equity_curve)

        bars = _universe_bars(context)
        z = np.random.default_rng(self._seed).standard_normal(len(bars))

        correlations: list[float] = []
        for sigma in self._sigmas:
            if sigma == 0.0:
                correlations.append(1.0)
                continue
            lake = _perturbed_lake(context, bars, z, sigma)
            try:
                report = run_backtest(strategy, context, context.full_window, lake=lake)
            finally:
                lake.close()
            correlations.append(_pearson(baseline, list(report.equity_curve)))

        mean_corr = sum(correlations) / len(correlations) if correlations else 0.0
        min_corr = min(correlations) if correlations else 0.0
        metrics = {
            "correlation_mean": mean_corr,
            "correlation_min": min_corr,
            "levels_tested": float(len(self._sigmas)),
        }
        passed = min_corr >= self._min_corr
        return SurvivalReport(
            test_id=self.id, passed=passed, metrics=metrics, notes=f"seed={self._seed}"
        )


def _universe_bars(context: LabDataset) -> pd.DataFrame:
    """Every bar (all intervals, all history up to the window end) for the
    universe, in a deterministic row order so noise draws are stable."""
    _, end = context.full_window
    return context.lake.sql(
        """
        SELECT ticker, timestamp, interval, open, high, low, close, adj_close, volume
          FROM bars
         WHERE ticker = ANY(?) AND CAST(timestamp AS DATE) <= ?
         ORDER BY ticker, interval, timestamp
        """,
        [list(context.universe), end],
    )


def _perturbed_lake(
    context: LabDataset, bars: pd.DataFrame, z: np.ndarray, sigma: float
) -> DuckDBLake:
    lake = copy_universe_lake(context.lake, context.universe)
    if bars.empty:
        return lake
    noisy = bars.copy()
    factor = np.exp(sigma * z)
    for col in _PRICE_COLS:
        noisy[col] = noisy[col].astype(float) * factor
    for code, frame in noisy.groupby("interval", sort=False):
        lake.upsert_bars(frame, interval=Interval.parse(code))
    return lake


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
