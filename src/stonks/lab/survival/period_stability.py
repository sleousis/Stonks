"""Period-stability test: backtest across K disjoint sub-windows and check
the spread and the floor of the per-window Sharpes.

Passing requires all of:

- ``pstdev(sharpes) <= max_sharpe_std`` (consistency),
- ``min(sharpes) >= min_period_sharpe`` (default 0.0 — no losing window),
- not every window's Sharpe exactly 0.0. A flat equity curve in every
  window means the strategy never traded (or never moved), which is no
  evidence of stability; with the ``>=`` floor alone it would pass.
"""

from __future__ import annotations

import statistics
from datetime import timedelta

from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.backtesting import run_backtest
from stonks.lab.dataset import LabDataset


class PeriodStabilityTest:
    id = "period_stability"

    def __init__(
        self,
        n_windows: int = 3,
        max_sharpe_std: float = 1.0,
        min_period_sharpe: float = 0.0,
    ) -> None:
        if n_windows < 2:
            raise ValueError("n_windows must be >= 2")
        self._n = n_windows
        self._max_std = max_sharpe_std
        self._min_sharpe = min_period_sharpe

    def run(self, strategy: Strategy, context: LabDataset) -> SurvivalReport:
        span = (context.end - context.start).days
        chunk = span // self._n
        sharpes: list[float] = []

        for i in range(self._n):
            start = context.start + timedelta(days=i * chunk)
            end = (
                context.start + timedelta(days=(i + 1) * chunk) if i < self._n - 1 else context.end
            )
            sharpes.append(run_backtest(strategy, context, (start, end)).sharpe)

        std = statistics.pstdev(sharpes) if len(sharpes) > 1 else 0.0
        sharpe_min = min(sharpes) if sharpes else 0.0
        metrics = {
            "sharpe_std": std,
            "sharpe_min": sharpe_min,
            "sharpe_max": max(sharpes) if sharpes else 0.0,
            "n_windows": float(self._n),
        }
        never_traded = all(s == 0.0 for s in sharpes)
        passed = std <= self._max_std and sharpe_min >= self._min_sharpe and not never_traded
        notes = "every window had a Sharpe of exactly 0 (no trading)" if never_traded else ""
        return SurvivalReport(test_id=self.id, passed=passed, metrics=metrics, notes=notes)
