"""Wald-Wolfowitz runs-test survival check.

Runs a backtest of the strategy over ``context.full_window``, takes the
sign of each bar's change in equity (skipping zeros), and computes the
runs-test Z-score for that ±1 sequence. A value near 0 indicates
independent, unpredictable per-bar outcomes; large positive Z means the
sequence over-alternates (too many runs), large negative Z means
wins/losses cluster (too few runs). Strategies whose per-bar outcomes
show strong dependence are flagged, since dependence often signals
regime lock-in that the backtest window happened to capture.
"""

from __future__ import annotations

import math

import numpy as np

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.interval import Interval
from stonks.core.protocols import Strategy, SurvivalReport
from stonks.core.types import Portfolio
from stonks.features.library import count_runs, runs_test_z_score
from stonks.lab.dataset import LabDataset


class RunsTestSurvivalTest:
    id = "runs_test"

    def __init__(self, max_abs_z_score: float = 3.0) -> None:
        if max_abs_z_score < 0.0:
            raise ValueError("max_abs_z_score must be >= 0")
        self._max_abs_z = max_abs_z_score

    def run(self, strategy: Strategy, context: LabDataset) -> SurvivalReport:
        interval = getattr(context, "interval", Interval.DAY_1)
        universe = list(context.universe)
        start, end = context.full_window

        broker = SimulatedBroker(portfolio=Portfolio(cash=10_000.0, positions={}))
        report = Backtester(
            strategies=[strategy], broker=broker, lake=context.lake,
            config=BacktestConfig(
                start=start, end=end, universe=universe,
                interval=interval, threshold=0.0,
            ),
        ).run()

        curve = np.asarray(report.equity_curve, dtype=float)
        if curve.size < 3:
            return SurvivalReport(
                test_id=self.id,
                passed=True,
                metrics={
                    "z_score": 0.0, "n_positive": 0.0, "n_negative": 0.0,
                    "n_runs": 0.0,
                },
                notes="insufficient equity-curve length for a runs test",
            )

        diffs = np.diff(curve)
        nonzero = diffs[diffs != 0]
        if nonzero.size < 2:
            return SurvivalReport(
                test_id=self.id,
                passed=True,
                metrics={
                    "z_score": 0.0, "n_positive": 0.0, "n_negative": 0.0,
                    "n_runs": 0.0,
                },
                notes="insufficient variance in per-bar returns",
            )

        signs = np.sign(nonzero).astype(int)
        z = runs_test_z_score(signs)
        n_pos = int((signs > 0).sum())
        n_neg = int((signs < 0).sum())
        n_runs = count_runs(signs)

        if math.isnan(z):
            return SurvivalReport(
                test_id=self.id,
                passed=True,
                metrics={
                    "z_score": float("nan"),
                    "n_positive": float(n_pos),
                    "n_negative": float(n_neg),
                    "n_runs": float(n_runs),
                },
                notes="degenerate sign sequence (all positive or all negative)",
            )

        passed = abs(z) <= self._max_abs_z
        return SurvivalReport(
            test_id=self.id,
            passed=passed,
            metrics={
                "z_score": float(z),
                "n_positive": float(n_pos),
                "n_negative": float(n_neg),
                "n_runs": float(n_runs),
            },
        )


