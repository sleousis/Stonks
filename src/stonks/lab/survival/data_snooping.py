"""Data-snooping survival test (roadmap 23.9, P2 and P3).

Does the best trial of the search beat cash once every trial tried is
counted? It runs White's Reality Check, Hansen's SPA and the Romano-Wolf
step-down (:mod:`stonks.stats.data_snooping`) over the per-bar returns of
every trial in the run's trial family: the run's own trials plus those of
every other run in the same research family (roadmap 22.9). It passes when
Hansen's consistent SPA p-value is at most ``max_p``. The Reality Check
p-value, how many trials Romano-Wolf rejects and whether it rejects the
selected trial are reported too.

Other runs' trials are lined up on the run's own bars by date. A bar a
trial did not cover counts as flat (a zero return, like holding cash), and
a trial with no usable bars is left out. Without per-bar trial returns or
with fewer than ``min_trials`` usable trials it fails for lack of data.

The bootstrap is one matrix product per resample set, so a few hundred
trials over a few thousand bars take well under a second.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.survival.deflated_sharpe import bound_trial_matrix, usable_trial_columns
from stonks.lab.trials import LabRunContext, TrialMatrix
from stonks.stats.data_snooping import data_snooping

__all__ = ["DataSnoopingTest", "family_returns"]


class DataSnoopingTest:
    id = "data_snooping"

    class Options(BaseModel):
        model_config = ConfigDict(extra="forbid")

        max_p: float = Field(
            0.05,
            gt=0.0,
            lt=1.0,
            description="Highest SPA p-value that passes. Also the Romano-Wolf error rate.",
        )
        n_boot: int = Field(1000, ge=100, le=20000, description="Bootstrap resamples.")
        mean_block: float = Field(
            10.0, ge=1.0, description="Mean block length in bars, for autocorrelated returns."
        )
        min_trials: int = Field(
            2, ge=1, description="Fewest usable trials to judge. Fewer fails for lack of data."
        )
        seed: int = 0

    def __init__(
        self,
        max_p: float = 0.05,
        n_boot: int = 1000,
        mean_block: float = 10.0,
        min_trials: int = 2,
        seed: int = 0,
    ) -> None:
        self.max_p = max_p
        self.n_boot = n_boot
        self.mean_block = mean_block
        self.min_trials = max(1, min_trials)
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
            "n_family_runs": 0.0,
            "n_bars": 0.0,
            "spa_p": nan,
            "spa_p_lower": nan,
            "spa_p_upper": nan,
            "reality_check_p": nan,
            "romano_wolf_rejected": 0.0,
            "selected_rejected": nan,
            "selected_p": nan,
        }
        ctx = self._ctx
        if ctx is None:
            return self._fail(metrics, "no trial data (bind_run was not called)")
        own = bound_trial_matrix(ctx)
        if own is None:
            return self._fail(metrics, "the tuner reported no per-bar trial returns")
        others: list[TrialMatrix] = []
        if ctx.family and ctx.ledger is not None:
            others = [
                m for rid, m in ctx.ledger.family_trial_matrices(ctx.family) if rid != ctx.run_id
            ]
        values = family_returns(own, others)
        usable = usable_trial_columns(values)
        metrics.update(
            n_trials=float(values.shape[1]),
            n_trials_usable=float(len(usable)),
            n_family_runs=float(1 + len(others)),
            n_bars=float(values.shape[0]),
        )
        if len(usable) < self.min_trials:
            return self._fail(metrics, f"{len(usable)} usable trials < {self.min_trials}")
        if values.shape[0] < 3:
            return self._fail(metrics, f"{values.shape[0]} bars < 3")

        d = np.nan_to_num(values[:, usable], nan=0.0)
        result = data_snooping(
            d, alpha=self.max_p, n_boot=self.n_boot, mean_block=self.mean_block, seed=self.seed
        )
        rw = result.romano_wolf
        metrics.update(
            spa_p=result.spa.p_consistent,
            spa_p_lower=result.spa.p_lower,
            spa_p_upper=result.spa.p_upper,
            reality_check_p=result.reality_check.p_value,
            romano_wolf_rejected=float(rw.n_rejected),
        )
        selected = _selected_column(ctx)
        if selected is not None and selected in usable:
            pos = usable.index(selected)
            metrics["selected_rejected"] = float(bool(rw.reject[pos]))
            metrics["selected_p"] = float(rw.p_values[pos])
        passed = result.spa.p_consistent <= self.max_p
        notes = (
            ""
            if passed
            else f"SPA p {result.spa.p_consistent:.3f} > {self.max_p}: the best of "
            f"{len(usable)} trials does not beat cash once they are all counted"
        )
        return SurvivalReport(test_id=self.id, passed=passed, metrics=metrics, notes=notes)

    def _fail(self, metrics: dict[str, float], reason: str) -> SurvivalReport:
        return SurvivalReport(
            test_id=self.id, passed=False, metrics=metrics, notes=f"insufficient data: {reason}"
        )


def family_returns(own: TrialMatrix, others: list[TrialMatrix]) -> np.ndarray:
    """``T x N`` per-bar returns on ``own``'s bars: its columns first, then
    each other matrix's columns lined up by date (NaN where a trial has no
    bar). Positional matrices can't be lined up, so only ``own`` is used
    when either side is not dated."""
    columns = [np.asarray(own.values, dtype=float)]
    if not np.issubdtype(own.index.dtype, np.datetime64):
        return columns[0]
    target = own.index.astype("datetime64[ns]")
    for other in others:
        if not np.issubdtype(other.index.dtype, np.datetime64):
            continue
        source = other.index.astype("datetime64[ns]")
        pos = np.searchsorted(source, target)
        pos_clipped = np.minimum(pos, max(len(source) - 1, 0))
        hit = (pos < len(source)) & (source[pos_clipped] == target) if len(source) else pos < 0
        block = np.full((target.size, other.values.shape[1]), np.nan)
        block[hit] = np.asarray(other.values, dtype=float)[pos_clipped[hit]]
        columns.append(block)
    return np.hstack(columns)


def _selected_column(ctx: LabRunContext) -> int | None:
    """The run's own column the tuner picked: its best-scored trial."""
    objective = getattr(ctx.setup, "objective", None)
    sign = -1.0 if getattr(objective, "direction", "maximize") == "minimize" else 1.0
    best: int | None = None
    best_score = -math.inf
    for trial in ctx.trials:
        if trial.status == "ok" and sign * trial.score > best_score:
            best, best_score = trial.trial_index, sign * trial.score
    return best
