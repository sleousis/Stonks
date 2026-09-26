"""Out-of-sample survival test: backtest on the held-out val window (BL-16).

The default gate is statistical: the probabilistic Sharpe ratio
``PSR(0) = P(true SR > 0)`` of the per-bar validation returns, with their
measured skew, kurtosis and lag-1 autocorrelation, must reach ``min_psr``,
and the trade ledger must hold at least ``min_trades`` closed trades. The
PSR and MinTRL variance is taken at the observed Sharpe (``stats.sharpe``),
so negative skew and fat tails lower the PSR and lengthen the MinTRL. A
six-month window at Sharpe 0.5 can't be told apart from zero, so a flat
Sharpe bar treats short and long windows alike (principle P8).

``mode="sharpe"`` keeps the legacy flat-Sharpe gate (``min_sharpe``). The
drawdown limit and the trade minimum apply in both modes; the exact legacy
rule is ``mode="sharpe", min_trades=0``.
"""

from __future__ import annotations

import math
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from stonks.backtest.report import BacktestReport
from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.backtesting import run_backtest
from stonks.lab.dataset import LabDataset
from stonks.stats.bootstrap import sharpe_ci
from stonks.stats.sharpe import ReturnMoments, min_trl, psr, return_moments, sharpe_variance

OOSMode = Literal["psr", "sharpe"]

#: Fewest validation returns the moments (skew, kurtosis, rho) need.
MIN_BARS = 3
#: Lag-1 autocorrelation is clipped here so the AR(1) Sharpe variance stays
#: finite on degenerate series.
_RHO_CLIP = 0.95


class OutOfSampleTest:
    id = "oos"

    class Options(BaseModel):
        model_config = ConfigDict(extra="forbid")

        mode: OOSMode = "psr"
        min_psr: float = Field(0.95, gt=0.0, lt=1.0)
        min_sharpe: float = 0.5
        #: Inclusive lower bound on the validation max drawdown (e.g. -0.3).
        max_drawdown_limit: float = Field(-0.3, le=0.0)
        min_trades: int = Field(20, ge=0)
        #: Two-sided level of the bootstrap Sharpe interval.
        ci_alpha: float = Field(0.05, gt=0.0, lt=1.0)
        n_boot: int = Field(1000, ge=100)
        seed: int = 0

    def __init__(
        self,
        min_sharpe: float = 0.5,
        max_drawdown_limit: float = -0.3,
        *,
        mode: OOSMode = "psr",
        min_psr: float = 0.95,
        min_trades: int = 20,
        ci_alpha: float = 0.05,
        n_boot: int = 1000,
        seed: int = 0,
    ) -> None:
        if mode not in ("psr", "sharpe"):
            raise ValueError(f"mode must be 'psr' or 'sharpe', got {mode!r}")
        self.mode: OOSMode = mode
        self.min_psr = min_psr
        self.min_sharpe = min_sharpe
        self.max_drawdown_limit = max_drawdown_limit
        self.min_trades = min_trades
        self.ci_alpha = ci_alpha
        self.n_boot = n_boot
        self.seed = seed

    def run(self, strategy: Strategy, context: LabDataset) -> SurvivalReport:
        return self.evaluate(run_backtest(strategy, context, context.val_window))

    def evaluate(self, report: BacktestReport) -> SurvivalReport:
        """Judge a validation-window backtest report."""
        returns = np.asarray(report.returns, dtype=float)
        mom = return_moments(returns)
        trades = report.trade_stats
        metrics: dict[str, float] = {
            "sharpe_oos": report.sharpe,
            "max_drawdown_oos": report.max_drawdown,
            "final_return_oos": report.final_return,
            "cagr_oos": report.cagr,
            "n_bars": float(mom.n),
            "n_trades": float(trades.n_trades),
            "trade_expectancy": trades.expectancy,
            **self._psr_metrics(returns, mom),
        }

        failures: list[str] = []
        if mom.n < MIN_BARS:
            failures.append(f"insufficient data: {mom.n} validation returns < {MIN_BARS}")
        elif self.mode == "psr":
            if not metrics["psr0"] >= self.min_psr:
                failures.append(f"PSR(0) {metrics['psr0']:.3f} < {self.min_psr}")
        elif report.sharpe < self.min_sharpe:
            failures.append(f"Sharpe {report.sharpe:.3f} < {self.min_sharpe}")
        if report.max_drawdown < self.max_drawdown_limit:
            failures.append(
                f"max drawdown {report.max_drawdown:.3f} below {self.max_drawdown_limit}"
            )
        if trades.n_trades < self.min_trades:
            failures.append(f"insufficient trades: {trades.n_trades} < {self.min_trades}")

        return SurvivalReport(
            test_id=self.id,
            passed=not failures,
            metrics=metrics,
            notes="; ".join(failures),
        )

    def _psr_metrics(self, returns: np.ndarray, mom: ReturnMoments) -> dict[str, float]:
        nan = float("nan")
        out = {
            "sr_per_bar": mom.sharpe,
            "skew": mom.skew,
            "kurtosis": mom.kurt,
            "rho": mom.rho,
            "sharpe_se": nan,
            "psr0": nan,
            "min_trl_bars": nan,
            "sharpe_ci_low": nan,
            "sharpe_ci_high": nan,
        }
        if mom.n < MIN_BARS:
            return out
        rho = float(np.clip(mom.rho, -_RHO_CLIP, _RHO_CLIP))
        var = sharpe_variance(mom.sharpe, mom.n, mom.skew, mom.kurt, rho)
        ci = sharpe_ci(returns, alpha=self.ci_alpha, n_boot=self.n_boot, seed=self.seed)
        out.update(
            sharpe_se=math.sqrt(var) if var > 0 else nan,
            psr0=psr(mom.sharpe, 0.0, mom.n, mom.skew, mom.kurt, rho),
            min_trl_bars=min_trl(mom.sharpe, 0.0, mom.skew, mom.kurt, rho),
            sharpe_ci_low=ci.lower,
            sharpe_ci_high=ci.upper,
        )
        return out
