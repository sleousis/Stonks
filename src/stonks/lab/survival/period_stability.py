"""Period-stability test: backtest across K consecutive sub-windows and
check the spread and the floor of the per-window Sharpes.

Neighbouring sub-windows share their boundary day: its bar is the last
mark of one window and the starting mark of the next, so no bar-to-bar
return is scored twice.

The sub-windows split the validation window (``window="val"``, the default
since BL-21: the embargoed window the tuner never saw, see
``lab.dataset.scoring_window``); ``window="full"`` splits the whole
dataset as before. A window shorter than ``n_windows`` days fails with an
"insufficient data" note.

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
from typing import ClassVar

from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.backtesting import run_backtest
from stonks.lab.dataset import LabDataset, ScoringWindow, scoring_window


class PeriodStabilityTest:
    id = "period_stability"

    #: Plain words for each option, shown by the console's options editor.
    option_help: ClassVar[dict[str, str]] = {
        "n_windows": "How many equal pieces to split the window into.",
        "max_sharpe_std": "Largest spread of Sharpe between pieces that passes.",
        "min_period_sharpe": "Lowest Sharpe any piece may have.",
        "window": "Which data to test on: val is the held-out window, full is all of it.",
    }

    def __init__(
        self,
        n_windows: int = 3,
        max_sharpe_std: float = 1.0,
        min_period_sharpe: float = 0.0,
        window: ScoringWindow = "val",
    ) -> None:
        if n_windows < 2:
            raise ValueError("n_windows must be >= 2")
        if window not in ("val", "full"):
            raise ValueError(f"window must be 'val' or 'full', got {window!r}")
        self._n = n_windows
        self._max_std = max_sharpe_std
        self._min_sharpe = min_period_sharpe
        self._window: ScoringWindow = window

    def run(self, strategy: Strategy, context: LabDataset) -> SurvivalReport:
        first, last = scoring_window(context, strategy, self._window)
        span = (last - first).days
        chunk = span // self._n
        if chunk < 1:
            return SurvivalReport(
                test_id=self.id,
                passed=False,
                metrics={"n_windows": float(self._n)},
                notes=(
                    f"window={self._window}; insufficient data: {span + 1} days "
                    f"cannot hold {self._n} sub-windows"
                ),
            )
        sharpes: list[float] = []

        for i in range(self._n):
            start = first + timedelta(days=i * chunk)
            end = first + timedelta(days=(i + 1) * chunk) if i < self._n - 1 else last
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
        notes = f"window={self._window}"
        if never_traded:
            notes += "; every window had a Sharpe of exactly 0 (no trading)"
        return SurvivalReport(test_id=self.id, passed=passed, metrics=metrics, notes=notes)
