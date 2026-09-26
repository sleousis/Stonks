"""Perturbation test: run a baseline backtest, then repeat with gaussian
noise added to prices and check how correlated the equity curves stay."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence

from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.backtesting import run_backtest
from stonks.lab.dataset import LabDataset
from stonks.store.lake import DuckDBLake


class PerturbationTest:
    id = "perturbation"

    def __init__(
        self,
        noise_sigmas: Sequence[float] = (0.0, 0.005, 0.01),
        min_correlation: float = 0.8,
        seed: int | None = None,
    ) -> None:
        self._sigmas = list(noise_sigmas)
        self._min_corr = min_correlation
        self._seed = seed

    def run(self, strategy: Strategy, context: LabDataset) -> SurvivalReport:
        baseline = _run(strategy, context, noise=0.0, seed=self._seed)
        correlations: list[float] = []
        for sigma in self._sigmas:
            if sigma == 0.0:
                correlations.append(1.0)
                continue
            curve = _run(strategy, context, noise=sigma, seed=self._seed)
            correlations.append(_pearson(baseline, curve))

        mean_corr = sum(correlations) / len(correlations) if correlations else 0.0
        min_corr = min(correlations) if correlations else 0.0
        metrics = {
            "correlation_mean": mean_corr,
            "correlation_min": min_corr,
            "levels_tested": float(len(self._sigmas)),
        }
        passed = min_corr >= self._min_corr
        return SurvivalReport(test_id=self.id, passed=passed, metrics=metrics)


def _run(strategy: Strategy, context: LabDataset, noise: float, seed: int | None) -> list[float]:
    lake: DuckDBLake = context.lake
    # wrap lake.get_prices to inject noise at read time
    if noise > 0.0:
        rng = random.Random(seed)
        original = lake.get_prices

        def noisy_get_prices(ticker, start, end):
            df = original(ticker, start, end).copy()
            df["close"] = df["close"] * df["close"].apply(
                lambda _x, rng=rng, n=noise: 1.0 + rng.gauss(0.0, n)
            )
            return df

        lake.get_prices = noisy_get_prices  # type: ignore[method-assign]

    try:
        report = run_backtest(strategy, context, context.full_window, lake=lake)
        return list(report.equity_curve)
    finally:
        if noise > 0.0:
            lake.get_prices = original  # type: ignore[method-assign]


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
