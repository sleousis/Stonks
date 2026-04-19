"""Out-of-sample survival test: backtest on the held-out val window."""

from __future__ import annotations

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.protocols import Strategy, SurvivalReport
from stonks.core.types import Portfolio
from stonks.lab.dataset import LabDataset


class OutOfSampleTest:
    id = "oos"

    def __init__(self, min_sharpe: float = 0.5, max_drawdown_limit: float = -0.3) -> None:
        self._min_sharpe = min_sharpe
        self._max_dd = max_drawdown_limit  # inclusive lower bound (e.g. -0.3)

    def run(self, strategy: Strategy, context: LabDataset) -> SurvivalReport:
        start, end = context.val_window
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

        metrics = {
            "sharpe_oos": report.sharpe,
            "max_drawdown_oos": report.max_drawdown,
            "final_return_oos": report.final_return,
            "cagr_oos": report.cagr,
        }
        passed = report.sharpe >= self._min_sharpe and report.max_drawdown >= self._max_dd
        return SurvivalReport(test_id=self.id, passed=passed, metrics=metrics)
