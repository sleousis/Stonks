"""Wald-Wolfowitz runs-test survival check.

Runs a backtest of the strategy over ``context.full_window`` and computes
the runs-test Z-score of a ±1 outcome sequence:

- **bar level (default)** — the sign of each bar's change in equity
  (skipping zeros);
- **trade level** (``trade_level=True``) — the sign of each closed round
  trip's return from the backtest's trade ledger
  (``BacktestReport.trades``, BL-02), read by :func:`round_trip_trades`
  (zero-return trades dropped). This is the sequence the original
  neurotrader888 check scores: whether wins and losses of consecutive
  trades are independent.

Trade semantics follow the ledger: FIFO lots, and each (lot, sell) pair
is one round trip. A partial exit therefore counts as its own trade (the
test's former private pairing merged all sells of a lot into one trade),
and lots still open at the end are not trades. Fees are inside each
trade's return either way; strategies that exit whole positions (every
catalogued one) score the same sequence as before.

A value near 0 indicates independent, unpredictable outcomes; large
positive Z means the sequence over-alternates (too many runs), large
negative Z means wins/losses cluster (too few runs). Strategies whose
outcomes show strong dependence are flagged, since dependence often
signals regime lock-in that the backtest window happened to capture.
"""

from __future__ import annotations

import math

import numpy as np

from stonks.backtest.report import BacktestReport
from stonks.backtest.trades import RoundTrip
from stonks.core.protocols import Strategy, SurvivalReport
from stonks.features.library import count_runs, runs_test_z_score
from stonks.lab.backtesting import run_backtest
from stonks.lab.dataset import LabDataset, ScoringWindow, scoring_window


def round_trip_trades(report: BacktestReport) -> list[RoundTrip]:
    """The report's closed round trips (its trade ledger), ordered by exit
    time; open lots are left out."""
    closed = [t for t in report.trades if not t.is_open]
    return sorted(closed, key=lambda t: t.exit_ts)


class RunsTestSurvivalTest:
    id = "runs_test"

    def __init__(
        self,
        max_abs_z_score: float = 3.0,
        trade_level: bool = False,
        window: ScoringWindow = "val",
    ) -> None:
        if max_abs_z_score < 0.0:
            raise ValueError("max_abs_z_score must be >= 0")
        if window not in ("val", "full"):
            raise ValueError(f"window must be 'val' or 'full', got {window!r}")
        self._max_abs_z = max_abs_z_score
        self._trade_level = trade_level
        self._window: ScoringWindow = window

    def run(self, strategy: Strategy, context: LabDataset) -> SurvivalReport:
        report = run_backtest(strategy, context, scoring_window(context, strategy, self._window))
        if self._trade_level:
            returns = np.array([t.return_pct for t in round_trip_trades(report)], dtype=float)
            return self._score(returns[returns != 0], level="trade")

        curve = np.asarray(report.equity_curve, dtype=float)
        if curve.size < 3:
            return self._skip(
                f"bar_level; window={self._window}; insufficient equity-curve length for a runs test",
                "bar",
            )
        diffs = np.diff(curve)
        return self._score(diffs[diffs != 0], level="bar")

    def _score(self, outcomes: np.ndarray, level: str) -> SurvivalReport:
        """Runs test on the signs of the non-zero ``outcomes``."""
        tag = ("trade_level" if level == "trade" else "bar_level") + f"; window={self._window}"
        if outcomes.size < 2:
            return self._skip(f"{tag}; insufficient variance in per-{level} returns", level)

        signs = np.sign(outcomes).astype(int)
        z = runs_test_z_score(signs)
        metrics = {
            "z_score": float(z),
            "n_positive": float((signs > 0).sum()),
            "n_negative": float((signs < 0).sum()),
            "n_runs": float(count_runs(signs)),
        }
        if level == "trade":
            metrics["n_trades"] = float(signs.size)

        if math.isnan(z):
            return SurvivalReport(
                test_id=self.id,
                passed=True,
                metrics=metrics,
                notes=f"{tag}; degenerate sign sequence (all positive or all negative)",
            )
        return SurvivalReport(
            test_id=self.id, passed=abs(z) <= self._max_abs_z, metrics=metrics, notes=tag
        )

    def _skip(self, notes: str, level: str) -> SurvivalReport:
        extra = {"n_trades": 0.0} if level == "trade" else {}
        return SurvivalReport(
            test_id=self.id,
            passed=True,
            metrics={
                "z_score": 0.0,
                "n_positive": 0.0,
                "n_negative": 0.0,
                "n_runs": 0.0,
                **extra,
            },
            notes=notes,
        )
