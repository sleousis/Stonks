"""Deflated Sharpe survival test (BL-14; Bailey & Lopez de Prado 2014).

The best of many noisy trials is biased upward (principle P3). The test
asks how likely the selected strategy's **validation** Sharpe is to beat
the Sharpe the best of the tried configurations would show with no skill:

- ``N`` is the cumulative trial count of the strategy class
  (``include_prior_runs``, since repeated lab runs are trials too) or this
  run's count; ``N_eff`` scales it by how independent the trials are,
  measured on the trial-return correlation (``n_eff_method``).
- ``V`` is the cross-trial variance of the per-bar trial Sharpes.
- ``SR0 = expected_max_sharpe(N_eff, V)`` and
  ``DSR = psr(SR, SR0, T, skew, kurt, rho)`` on the validation returns.

It passes at ``DSR >= min_dsr``. The trials come from the run through the
``bind_run(ctx)`` hook (``ctx.trial_matrix``, else the ledger's saved
matrix); with no trial returns to deflate by, the test fails with an
"insufficient data" note rather than passing. With ``N = 1`` the DSR is
``PSR(0)``.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.backtesting import run_backtest
from stonks.lab.trials import LabRunContext, TrialMatrix
from stonks.stats.sharpe import (
    NEffMethod,
    deflated_sharpe_inputs,
    psr,
    return_moments,
    sharpe_variance,
)

__all__ = ["DeflatedSharpeTest", "bound_trial_matrix", "usable_trial_columns"]

#: Fewest validation returns the moments (skew, kurtosis, rho) need.
MIN_BARS = 3
_RHO_CLIP = 0.95


class DeflatedSharpeTest:
    id = "deflated_sharpe"

    class Options(BaseModel):
        model_config = ConfigDict(extra="forbid")

        min_dsr: float = Field(
            0.95,
            ge=0.8,
            le=0.99,
            description="Lowest deflated Sharpe probability that passes. It discounts luck from trying many settings.",
        )
        include_prior_runs: bool = Field(
            default=True,
            description="Count the settings tried in earlier lab runs of this strategy too.",
        )
        n_eff_method: NEffMethod = Field(
            default="effective_rank",
            description="How to count trials that behave alike as fewer independent ones.",
        )

    def __init__(
        self,
        min_dsr: float = 0.95,
        include_prior_runs: bool = True,
        n_eff_method: NEffMethod = "effective_rank",
    ) -> None:
        self.min_dsr = min_dsr
        self.include_prior_runs = include_prior_runs
        self.n_eff_method: NEffMethod = n_eff_method
        self._ctx: LabRunContext | None = None

    def bind_run(self, ctx: LabRunContext) -> None:
        self._ctx = ctx

    def run(self, strategy: Strategy, context: Any) -> SurvivalReport:
        report = run_backtest(strategy, context, context.val_window)
        return self.evaluate(np.asarray(report.returns, dtype=float))

    def evaluate(self, val_returns: np.ndarray) -> SurvivalReport:
        """Judge the selected strategy's per-bar validation returns."""
        nan = float("nan")
        mom = return_moments(val_returns)
        rho = float(np.clip(mom.rho, -_RHO_CLIP, _RHO_CLIP))
        metrics: dict[str, float] = {
            "n_trials": 0.0,
            "n_trials_run": 0.0,
            "n_trials_usable": 0.0,
            "n_eff": nan,
            "var_sr": nan,
            "sr0_per_bar": nan,
            "sr_per_bar": mom.sharpe,
            "sharpe_se": nan,
            "skew": mom.skew,
            "kurtosis": mom.kurt,
            "rho": mom.rho,
            "n_bars": float(mom.n),
            "psr0": nan,
            "dsr": nan,
        }
        if mom.n >= MIN_BARS:
            var = sharpe_variance(mom.sharpe, mom.n, mom.skew, mom.kurt, rho)
            metrics["sharpe_se"] = math.sqrt(var) if var > 0 else nan
            metrics["psr0"] = psr(mom.sharpe, 0.0, mom.n, mom.skew, mom.kurt, rho)

        ctx = self._ctx
        if ctx is None:
            return self._fail(metrics, "no trial data (bind_run was not called)")
        n_trials = ctx.n_trials_class if self.include_prior_runs else ctx.n_trials_run
        metrics["n_trials"] = float(n_trials)
        metrics["n_trials_run"] = float(ctx.n_trials_run)
        if n_trials < 1:
            return self._fail(metrics, "the run recorded no trials")
        matrix = bound_trial_matrix(ctx)
        if matrix is None:
            return self._fail(metrics, "the tuner reported no per-bar trial returns")
        usable = usable_trial_columns(matrix.values)
        metrics["n_trials_usable"] = float(len(usable))
        if not usable:
            return self._fail(metrics, "every trial failed or was flat")
        if len(usable) < 2 and n_trials > 1:
            # One column can't measure the trials' dispersion (V), so SR0
            # would collapse to 0 and skip the deflation entirely.
            return self._fail(
                metrics, f"only {len(usable)} usable trial return series for {n_trials} trials"
            )
        if mom.n < MIN_BARS:
            return self._fail(metrics, f"{mom.n} validation returns < {MIN_BARS}")

        inputs = deflated_sharpe_inputs(
            matrix.values[:, usable], self.n_eff_method, n_trials=n_trials
        )
        dsr = psr(mom.sharpe, inputs.sr0, mom.n, mom.skew, mom.kurt, rho)
        metrics.update(n_eff=inputs.n_eff, var_sr=inputs.var_sr, sr0_per_bar=inputs.sr0, dsr=dsr)
        passed = dsr >= self.min_dsr
        notes = "" if passed else f"DSR {dsr:.3f} < {self.min_dsr}"
        return SurvivalReport(test_id=self.id, passed=passed, metrics=metrics, notes=notes)

    def _fail(self, metrics: dict[str, float], reason: str) -> SurvivalReport:
        return SurvivalReport(
            test_id=self.id, passed=False, metrics=metrics, notes=f"insufficient data: {reason}"
        )


def bound_trial_matrix(ctx: LabRunContext) -> TrialMatrix | None:
    """The run's per-bar trial returns: ``ctx.trial_matrix``, else the one
    the ledger saved for ``ctx.run_id``; ``None`` when neither exists."""
    if ctx.trial_matrix is not None:
        return ctx.trial_matrix
    if ctx.ledger is not None:
        return ctx.ledger.trial_matrix(ctx.run_id)
    return None


def usable_trial_columns(values: np.ndarray) -> list[int]:
    """Indices of trial columns with at least 3 finite, non-constant bars
    (failed and flat trials carry no Sharpe to compare)."""
    out = []
    for j in range(values.shape[1]):
        col = values[:, j]
        finite = col[np.isfinite(col)]
        if finite.size > 2 and float(finite.std()) > 0:
            out.append(j)
    return out
