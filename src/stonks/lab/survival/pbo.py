"""Probability-of-backtest-overfitting survival test (BL-15).

Combinatorially symmetric cross-validation (Bailey, Borwein, Lopez de
Prado & Zhu 2015) over the run's train-window trial return matrix: for
every split of the bars into two halves of blocks, is the in-sample best
trial also above the out-of-sample median? PBO is the share of splits
where it is not. It passes at ``PBO <= max_pbo``. The math is
:func:`stonks.stats.pbo.cscv`; this module only binds it to the run.

The trials come through the ``bind_run(ctx)`` hook, so the test judges the
search, not the selected strategy. Without enough trial data (no matrix,
fewer than ``min_trials`` usable trials, fewer than 2 bars per block) it
fails with an "insufficient data" note rather than passing.

The combinations are capped at ``max_combinations`` (seeded sampling) and
evaluated vectorised in-process; at that size no worker pool is needed.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.survival.deflated_sharpe import bound_trial_matrix, usable_trial_columns
from stonks.lab.trials import LabRunContext
from stonks.stats.pbo import cscv

__all__ = ["PBOTest"]


class PBOTest:
    id = "pbo"

    class Options(BaseModel):
        model_config = ConfigDict(extra="forbid")

        n_blocks: int = Field(10, ge=8, le=16)
        max_combinations: int = Field(5000, ge=1)
        max_pbo: float = Field(0.2, ge=0.0, le=1.0)
        min_trials: int = Field(8, ge=2)
        seed: int = 0

        @field_validator("n_blocks")
        @classmethod
        def _even(cls, v: int) -> int:
            if v % 2:
                raise ValueError(f"n_blocks must be even, got {v}")
            return v

    def __init__(
        self,
        n_blocks: int = 10,
        max_combinations: int = 5000,
        max_pbo: float = 0.2,
        min_trials: int = 8,
        seed: int = 0,
    ) -> None:
        if n_blocks < 2 or n_blocks % 2:
            raise ValueError(f"n_blocks must be an even number >= 2, got {n_blocks}")
        self.n_blocks = n_blocks
        self.max_combinations = max_combinations
        self.max_pbo = max_pbo
        self.min_trials = max(2, min_trials)
        self.seed = seed
        self._ctx: LabRunContext | None = None

    def bind_run(self, ctx: LabRunContext) -> None:
        self._ctx = ctx

    def run(self, strategy: Strategy, context: Any) -> SurvivalReport:
        return self.evaluate()

    def evaluate(self) -> SurvivalReport:
        nan = float("nan")
        metrics: dict[str, float] = {
            "n_trials": 0.0,
            "n_trials_usable": 0.0,
            "n_bars": 0.0,
            "n_blocks": float(self.n_blocks),
            "pbo": nan,
            "degradation_slope": nan,
            "p_loss": nan,
            "n_combinations": 0.0,
        }
        ctx = self._ctx
        if ctx is None:
            return self._fail(metrics, "no trial data (bind_run was not called)")
        metrics["n_trials"] = float(ctx.n_trials_run)
        matrix = bound_trial_matrix(ctx)
        if matrix is None:
            return self._fail(metrics, "the tuner reported no per-bar trial returns")
        values = matrix.values
        usable = usable_trial_columns(values)
        metrics["n_trials_usable"] = float(len(usable))
        metrics["n_bars"] = float(values.shape[0])
        if len(usable) < self.min_trials:
            return self._fail(metrics, f"{len(usable)} usable trials < {self.min_trials}")
        if values.shape[0] < 2 * self.n_blocks:
            return self._fail(
                metrics,
                f"{values.shape[0]} bars < {2 * self.n_blocks} (2 per block, {self.n_blocks} blocks)",
            )

        result = cscv(values[:, usable], self.n_blocks, self.max_combinations, self.seed)
        metrics.update(
            pbo=result.pbo,
            degradation_slope=result.degradation_slope,
            p_loss=result.p_loss,
            n_combinations=float(result.n_combinations),
        )
        passed = result.pbo <= self.max_pbo
        notes = "" if passed else f"PBO {result.pbo:.3f} > {self.max_pbo}"
        return SurvivalReport(test_id=self.id, passed=passed, metrics=metrics, notes=notes)

    def _fail(self, metrics: dict[str, float], reason: str) -> SurvivalReport:
        return SurvivalReport(
            test_id=self.id, passed=False, metrics=metrics, notes=f"insufficient data: {reason}"
        )
