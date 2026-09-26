"""Parameter plateau test (BL-19, after Kaufman and Ehlers).

A robust parameter set sits on a plateau, not a peak: nudging any
parameter a little should cost little. The test steps each tunable
numeric parameter of the tuned strategy one at a time by ``+/- step`` of
its range (clipped to its bounds; an int moves at least one unit), so a
strategy with ``k`` such parameters gets up to ``2k`` neighbours.
Categorical and bool parameters, and parameters pinned for the run
(``TuningSetup.fixed_params``), are never varied.

Each neighbour, and the tuned params themselves, is rebuilt, fitted and
scored on the train window with the run's objective and on the
validation window with Sharpe. It passes when:

- ``median(neighbour train score) / best train score >= min_train_ratio``
  (for a ``minimize`` objective, ``best / median``); a best train score
  that is not positive leaves the ratio undefined and fails;
- ``median(neighbour OOS Sharpe) >= min_oos_sharpe_fraction * best OOS
  Sharpe``.

Fewer than ``min_neighbours`` scorable neighbours (no numeric tunable
parameter, all pinned, or neighbours that fail to build) is insufficient
data and fails, as is a tuned strategy that makes no trade in the
validation window (every OOS Sharpe would be 0 and the OOS criterion
would pass vacuously). A neighbour whose backtest raises is left out of the
medians and counted in ``n_failed_neighbours``.

From the run's trial ledger (``bind_run``) it also reports Ehlers'
robustness ratio (median trial score / best trial score) and the share of
profitable trials (score > 0); ``min_robustness_ratio`` optionally gates
on the former.

The objective comes from ``bind_run`` (or ``bind_tuning``); without a run
it falls back to ``SharpeObjective`` and says so. Neighbours run on the
lab process pool (``lab.parallel`` via ``_reruns``); nothing is random,
so the report does not depend on ``max_workers``.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from stonks.core.params import ParamSpace
from stonks.core.protocols import Objective, Strategy, SurvivalReport
from stonks.lab.objectives import SharpeObjective
from stonks.lab.survival._reruns import Rerun, run_reruns
from stonks.logging import get_logger

_log = get_logger("stonks.lab.survival.plateau")


class PlateauOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Step as a fraction of each numeric parameter's range.
    step: float = Field(default=0.15, ge=0.05, le=0.3)
    min_train_ratio: float = Field(default=0.7, ge=0.5, le=0.9)
    min_oos_sharpe_fraction: float = Field(default=0.5, ge=0.0, le=1.0)
    min_neighbours: int = Field(default=2, ge=1)
    #: Optional gate on Ehlers' robustness ratio; ``None`` only reports it.
    min_robustness_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    #: Worker processes; ``None`` means ``lab.parallel.default_max_workers()``.
    max_workers: int | None = Field(default=None, ge=1)


def plateau_neighbours(
    space: ParamSpace,
    best: Mapping[str, Any],
    *,
    step: float,
    pinned: Collection[str] = (),
) -> list[tuple[str, Any, dict[str, Any]]]:
    """``(name, value, params)`` for every one-at-a-time neighbour of
    ``best``: each tunable, unpinned int/float parameter with numeric
    bounds moved down then up by ``step`` of its range, clipped to the
    bounds. Ints move at least one unit; a move that lands on ``best``
    (at a bound) is dropped."""
    out: list[tuple[str, Any, dict[str, Any]]] = []
    for spec in space:
        if not spec.tunable or spec.name in pinned or spec.kind not in ("int", "float"):
            continue
        bounds = spec.bounds
        if not isinstance(bounds, tuple | list) or len(bounds) != 2:
            continue
        lo, hi = bounds
        current = best.get(spec.name, spec.default)
        if current is None:
            continue
        delta = step * (hi - lo)
        for sign in (-1, 1):
            value: Any = min(max(current + sign * delta, lo), hi)
            if spec.kind == "int":
                value = int(round(value))
                if value == current:
                    value = min(max(current + sign, lo), hi)
            if value == current:
                continue
            out.append((spec.name, value, {**best, spec.name: value}))
    return out


class PlateauTest:
    id = "plateau"
    Options = PlateauOptions

    def __init__(self, options: PlateauOptions | None = None, **overrides: Any) -> None:
        base = options or PlateauOptions()
        self.options = (
            PlateauOptions.model_validate({**base.model_dump(), **overrides}) if overrides else base
        )
        self._objective: Objective | None = None
        self._pinned: frozenset[str] = frozenset()
        self._trial_scores: list[float] | None = None

    @classmethod
    def build(cls, options: PlateauOptions) -> PlateauTest:
        return cls(options)

    def bind_tuning(self, setup: Any) -> None:
        self._objective = setup.objective
        self._pinned = frozenset(getattr(setup, "fixed_params", {}) or {})

    def bind_run(self, ctx: Any) -> None:
        self.bind_tuning(ctx.setup)
        self._trial_scores = [float(t.score) for t in ctx.trials]

    def run(self, strategy: Strategy, context: Any) -> SurvivalReport:
        opts = self.options
        objective = self._objective or SharpeObjective()
        maximize = getattr(objective, "direction", "maximize") == "maximize"
        best = dict(getattr(strategy, "params", None) or {})
        neighbours = plateau_neighbours(
            type(strategy).parameter_spec(), best, step=opts.step, pinned=self._pinned
        )
        notes = [f"objective={getattr(objective, 'name', type(objective).__name__)}"]
        metrics: dict[str, float] = {"step": opts.step, **self._ledger_metrics(maximize)}
        if len(neighbours) < opts.min_neighbours:
            metrics["n_neighbours"] = float(len(neighbours))
            notes.insert(
                0,
                f"insufficient data: {len(neighbours)} neighbours, {opts.min_neighbours} "
                "required (no unpinned numeric tunable parameter to step)",
            )
            return SurvivalReport(self.id, False, metrics, "; ".join(notes))

        _log.info("plateau.start", neighbours=len(neighbours), step=opts.step)
        reruns = [
            Rerun(params=p, score_objective=True) for p in [best, *(n[2] for n in neighbours)]
        ]
        results = run_reruns(
            strategy,
            context,
            reruns,
            objective=objective,
            max_workers=opts.max_workers,
            log_prefix="plateau",
        )
        head, rest = results[0], results[1:]
        if not head.ok or head.objective_score is None:
            notes.insert(0, f"tuned params failed to backtest: {head.error}")
            return SurvivalReport(self.id, False, metrics, "; ".join(notes))
        scored = [r for r in rest if r.ok and r.objective_score is not None]
        metrics["n_neighbours"] = float(len(scored))
        metrics["n_failed_neighbours"] = float(len(rest) - len(scored))
        best_train, best_oos = float(head.objective_score), float(head.sharpe)
        metrics["best_train_score"] = best_train
        metrics["best_oos_sharpe"] = best_oos
        if len(scored) < opts.min_neighbours:
            notes.insert(
                0,
                f"insufficient data: {len(scored)} neighbours scored, "
                f"{opts.min_neighbours} required",
            )
            return SurvivalReport(self.id, False, metrics, "; ".join(notes))
        if head.n_round_trips == 0:
            notes.insert(
                0,
                "insufficient data: the tuned params make no trade in the validation "
                "window, so the OOS criterion has nothing to compare",
            )
            return SurvivalReport(self.id, False, metrics, "; ".join(notes))

        train_median = float(np.median([r.objective_score for r in scored]))
        oos_median = float(np.median([r.sharpe for r in scored]))
        metrics["neighbour_train_median"] = train_median
        metrics["neighbour_oos_sharpe_median"] = oos_median

        failures: list[str] = []
        ratio = _train_ratio(best_train, train_median, maximize)
        if ratio is None:
            failures.append(
                f"best train score {best_train:.4g} is not positive; plateau ratio undefined"
            )
        else:
            metrics["train_ratio"] = ratio
            if ratio < opts.min_train_ratio:
                failures.append(f"train ratio {ratio:.3f} < {opts.min_train_ratio}")
        if oos_median < opts.min_oos_sharpe_fraction * best_oos:
            failures.append(
                f"neighbour OOS Sharpe median {oos_median:.3f} < "
                f"{opts.min_oos_sharpe_fraction} x best {best_oos:.3f}"
            )
        robustness = metrics.get("robustness_ratio")
        if opts.min_robustness_ratio is not None and (
            robustness is None or robustness < opts.min_robustness_ratio
        ):
            failures.append(
                f"robustness ratio {robustness} < {opts.min_robustness_ratio}"
                if robustness is not None
                else "robustness ratio unavailable (no trial ledger)"
            )
        notes.insert(0, "; ".join(failures) if failures else "tuned params sit on a plateau")
        return SurvivalReport(self.id, not failures, metrics, "; ".join(notes))

    def _ledger_metrics(self, maximize: bool) -> dict[str, float]:
        if self._trial_scores is None:
            return {}
        total = len(self._trial_scores)
        scores = np.array([s for s in self._trial_scores if math.isfinite(s)], dtype=float)
        out = {"n_trials": float(total)}
        if scores.size == 0:
            return out
        # a failed trial is not a profitable one
        out["share_profitable_trials"] = float((scores > 0).sum() / total)
        best = float(scores.max() if maximize else scores.min())
        ratio = _train_ratio(best, float(np.median(scores)), maximize)
        if ratio is not None:
            out["robustness_ratio"] = ratio
        return out


def _train_ratio(best: float, median: float, maximize: bool) -> float | None:
    """How much of the best score the median keeps: ``median / best`` when
    maximizing, ``best / median`` when minimizing; ``None`` when the
    denominator is not positive."""
    if maximize:
        return median / best if best > 0 else None
    return best / median if median > 0 else None
