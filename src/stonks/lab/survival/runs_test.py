"""Wald-Wolfowitz runs-test survival check.

Runs a backtest of the strategy over ``context.full_window`` and computes
the runs-test Z-score of a ±1 outcome sequence:

- **bar level (default)** — the sign of each bar's change in equity
  (skipping zeros);
- **trade level** (``trade_level=True``) — the sign of each round-trip
  trade's return, reconstructed from the backtest's fills by
  :func:`round_trip_trades` (zero-return trades dropped). This is the
  sequence the original neurotrader888 check scores: whether wins and
  losses of consecutive trades are independent.

A value near 0 indicates independent, unpredictable outcomes; large
positive Z means the sequence over-alternates (too many runs), large
negative Z means wins/losses cluster (too few runs). Strategies whose
outcomes show strong dependence are flagged, since dependence often
signals regime lock-in that the backtest window happened to capture.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from stonks.core.protocols import Strategy, SurvivalReport
from stonks.core.types import Fill
from stonks.features.library import count_runs, runs_test_z_score
from stonks.lab.backtesting import run_backtest_with_fills
from stonks.lab.dataset import LabDataset

#: Relative size below which a lot remainder is float dust, not a position.
_DUST = 1e-9


@dataclass(frozen=True)
class RoundTrip:
    """One closed long lot: bought by one fill, sold by one or more."""

    ticker: str
    entry_at: datetime
    exit_at: datetime
    quantity: float
    #: Buy notional plus the buy fee.
    cost: float
    #: Sell notional minus the sell fees, pro rata to the quantity sold.
    proceeds: float

    @property
    def return_pct(self) -> float:
        return self.proceeds / self.cost - 1.0 if self.cost > 0 else 0.0


@dataclass
class _Lot:
    entry_at: datetime
    quantity: float
    remaining: float
    cost: float
    proceeds: float = 0.0


def round_trip_trades(fills: Iterable[Fill]) -> list[RoundTrip]:
    """Round-trip trades from ``fills`` (in fill order), long-only.

    Every buy opens a lot; sells close lots first-in-first-out per ticker,
    so a partial exit adds to the oldest open lot's proceeds and one sell
    can close several lots. A lot becomes a trade once fully sold. Fees
    count: the buy fee goes into the lot's cost, a sell fee is split over
    the lots it closes pro rata to quantity. Sell quantity beyond the open
    lots (a short, which this long-only view doesn't model) is ignored, and
    lots still open at the end are not trades. Trades come out ordered by
    exit time.
    """
    open_lots: dict[str, deque[_Lot]] = {}
    trades: list[RoundTrip] = []
    for fill in fills:
        if fill.quantity <= 0:
            continue
        lots = open_lots.setdefault(fill.ticker, deque())
        if fill.side == "buy":
            lots.append(
                _Lot(
                    entry_at=fill.filled_at,
                    quantity=fill.quantity,
                    remaining=fill.quantity,
                    cost=fill.quantity * fill.price + fill.fee,
                )
            )
            continue
        to_sell = fill.quantity
        while lots and to_sell > _DUST * fill.quantity:
            lot = lots[0]
            qty = min(lot.remaining, to_sell)
            lot.proceeds += qty * fill.price - fill.fee * qty / fill.quantity
            lot.remaining -= qty
            to_sell -= qty
            if lot.remaining <= _DUST * lot.quantity:
                lots.popleft()
                trades.append(
                    RoundTrip(
                        ticker=fill.ticker,
                        entry_at=lot.entry_at,
                        exit_at=fill.filled_at,
                        quantity=lot.quantity,
                        cost=lot.cost,
                        proceeds=lot.proceeds,
                    )
                )
    return trades


class RunsTestSurvivalTest:
    id = "runs_test"

    def __init__(self, max_abs_z_score: float = 3.0, trade_level: bool = False) -> None:
        if max_abs_z_score < 0.0:
            raise ValueError("max_abs_z_score must be >= 0")
        self._max_abs_z = max_abs_z_score
        self._trade_level = trade_level

    def run(self, strategy: Strategy, context: LabDataset) -> SurvivalReport:
        report, fills = run_backtest_with_fills(strategy, context, context.full_window)
        if self._trade_level:
            returns = np.array([t.return_pct for t in round_trip_trades(fills)], dtype=float)
            return self._score(returns[returns != 0], level="trade")

        curve = np.asarray(report.equity_curve, dtype=float)
        if curve.size < 3:
            return self._skip("bar_level; insufficient equity-curve length for a runs test", "bar")
        diffs = np.diff(curve)
        return self._score(diffs[diffs != 0], level="bar")

    def _score(self, outcomes: np.ndarray, level: str) -> SurvivalReport:
        """Runs test on the signs of the non-zero ``outcomes``."""
        tag = "trade_level" if level == "trade" else "bar_level"
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
