"""Period-stability test: backtest across K disjoint sub-windows and check
the spread of Sharpes."""

from __future__ import annotations

import statistics
from datetime import timedelta

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.protocols import Strategy, SurvivalReport
from stonks.core.types import Portfolio
from stonks.lab.dataset import LabDataset


class PeriodStabilityTest:
    id = "period_stability"

    def __init__(self, n_windows: int = 3, max_sharpe_std: float = 1.0) -> None:
        if n_windows < 2:
            raise ValueError("n_windows must be >= 2")
        self._n = n_windows
        self._max_std = max_sharpe_std

    def run(self, strategy: Strategy, context: LabDataset) -> SurvivalReport:
        span = (context.end - context.start).days
        chunk = span // self._n
        sharpes: list[float] = []

        for i in range(self._n):
            start = context.start + timedelta(days=i * chunk)
            end = (
                context.start + timedelta(days=(i + 1) * chunk) if i < self._n - 1 else context.end
            )
            broker = SimulatedBroker(portfolio=Portfolio(cash=10_000.0, positions={}))
            report = Backtester(
                strategies=[strategy],
                broker=broker,
                lake=context.lake,
                config=BacktestConfig(
                    start=start,
                    end=end,
                    universe=list(context.universe),
                    threshold=0.0,
                ),
            ).run()
            sharpes.append(report.sharpe)

        std = statistics.pstdev(sharpes) if len(sharpes) > 1 else 0.0
        metrics = {
            "sharpe_std": std,
            "sharpe_min": min(sharpes) if sharpes else 0.0,
            "sharpe_max": max(sharpes) if sharpes else 0.0,
            "n_windows": float(self._n),
        }
        passed = std <= self._max_std
        return SurvivalReport(test_id=self.id, passed=passed, metrics=metrics)
