"""Parameter heatmaps (22.5): a 2D sweep of two parameters around the
tuned set, with the plateau test's verdict laid over it.

The tuner picks a winner. The heatmap then shows the ground around it:
two parameters (``x`` and ``y``) swept over ``grid_size`` points each,
every other parameter held at its tuned value, and the tuned value added
to each axis so its own cell is on the map. A real effect sits on a broad
patch of good cells. A lone bright cell is fitted noise (P4).

Cells are scored one of two ways:

- **fast** (default, when the strategy has the vectorised fast path,
  ``target_positions``): the approximate backtest of ``lab/vectorized.py``
  on the train window, reported as ``fast_sharpe``. A cheap map, not a
  score the tuner uses;
- **full**: the run's objective on the train window, through the lab
  pool, like any tuning trial.

Every cell is a trial (P2). A full cell is an ordinary trial. A fast cell
comes back as a failed trial whose error starts with :data:`HEATMAP_FAST`,
like the pre-screen's screened-out sets: the ledger, the deflated Sharpe
and PBO count it, but its approximate score never mixes with real ones.
The sweep never changes the winner.

:func:`plateau_overlay` adds the plateau test's verdict and the ranges its
neighbours are drawn from (``best +/- step`` of each numeric axis's range).
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stonks.core.params import ParameterSpec, ParamSpace
from stonks.core.protocols import Objective, Strategy, SurvivalReport, TrialOutcome
from stonks.lab.parallel import ParallelSettings
from stonks.lab.tuning.base import evaluate_candidates, grid_axes, merge_with_defaults
from stonks.logging import get_logger

_log = get_logger("stonks.lab.heatmap")

__all__ = [
    "FAST_METRIC",
    "HEATMAP_FAST",
    "HeatmapOptions",
    "ParameterHeatmap",
    "PlateauOverlay",
    "pick_axes",
    "plateau_overlay",
    "sweep_heatmap",
]

#: Error prefix of a cell scored on the fast path (a counted trial, P2).
HEATMAP_FAST = "heatmap_fast"
#: The metric a fast map shows.
FAST_METRIC = "fast_sharpe"
#: Flat cost per unit turnover charged to the fast score.
_FAST_COST_BPS = 5.0
_NUMERIC = ("int", "float")


class HeatmapOptions(BaseModel):
    """Which two parameters to sweep and how finely."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    x: str | None = Field(
        default=None,
        max_length=64,
        description="Parameter across the map. Default: the first numeric tunable one.",
    )
    y: str | None = Field(
        default=None,
        max_length=64,
        description="Parameter down the map. Default: the next numeric tunable one.",
    )
    grid_size: int = Field(default=7, ge=2, le=15, description="Points per axis.")
    fast: bool = Field(
        default=True,
        description="Score cells on the vectorised fast path when the strategy has one.",
    )


@dataclass(frozen=True)
class PlateauOverlay:
    """The plateau test's verdict on the tuned set, and the ranges its
    neighbours come from (``None`` for a non-numeric axis)."""

    passed: bool
    notes: str
    step: float
    x_range: tuple[float, float] | None
    y_range: tuple[float, float] | None
    metrics: dict[str, float] = dataclasses.field(default_factory=dict)


@dataclass(frozen=True)
class ParameterHeatmap:
    """Scores over ``y_values`` (rows) by ``x_values`` (columns); NaN for a
    cell that failed. ``best`` is the tuned point, ``fixed`` the other
    params the sweep held."""

    x: str
    y: str
    x_values: list[Any]
    y_values: list[Any]
    scores: list[list[float]]
    metric: str
    fast: bool
    best: dict[str, Any]
    fixed: dict[str, Any]
    plateau: PlateauOverlay | None = None

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["scores"] = [[v if math.isfinite(v) else None for v in row] for row in self.scores]
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ParameterHeatmap:
        plateau = data.get("plateau")
        return cls(
            x=str(data["x"]),
            y=str(data["y"]),
            x_values=list(data["x_values"]),
            y_values=list(data["y_values"]),
            scores=[
                [float("nan") if v is None else float(v) for v in row] for row in data["scores"]
            ],
            metric=str(data["metric"]),
            fast=bool(data["fast"]),
            best=dict(data.get("best") or {}),
            fixed=dict(data.get("fixed") or {}),
            plateau=(
                PlateauOverlay(
                    passed=bool(plateau["passed"]),
                    notes=str(plateau.get("notes", "")),
                    step=float(plateau["step"]),
                    x_range=_pair(plateau.get("x_range")),
                    y_range=_pair(plateau.get("y_range")),
                    metrics={k: float(v) for k, v in (plateau.get("metrics") or {}).items()},
                )
                if plateau
                else None
            ),
        )


def _pair(value: Any) -> tuple[float, float] | None:
    if value is None:
        return None
    lo, hi = value
    return float(lo), float(hi)


def pick_axes(
    space: ParamSpace, options: HeatmapOptions, pinned: Collection[str] = ()
) -> tuple[ParameterSpec, ParameterSpec] | None:
    """The ``x`` and ``y`` specs: the named ones (``ValueError`` when
    unknown, not tunable, pinned or the same), else the first two tunable,
    unpinned parameters, numeric ones first. ``None`` with fewer than two."""
    by_name = {s.name: s for s in space}
    for name in (options.x, options.y):
        if name is None:
            continue
        if name not in by_name:
            raise ValueError(f"unknown parameter {name!r}; choose from {sorted(by_name)}")
        if not by_name[name].tunable:
            raise ValueError(f"parameter {name!r} is not tunable")
        if name in pinned:
            raise ValueError(f"parameter {name!r} is pinned for this run")
    if options.x is not None and options.x == options.y:
        raise ValueError("pick two different parameters for the heatmap")
    free = [
        s
        for s in space
        if s.tunable and s.name not in pinned and (s.kind != "categorical" or s.bounds)
    ]
    ordered = [s for s in free if s.kind in _NUMERIC] + [s for s in free if s.kind not in _NUMERIC]
    rest = iter(s for s in ordered if s.name not in (options.x, options.y))
    x = by_name[options.x] if options.x is not None else next(rest, None)
    y = by_name[options.y] if options.y is not None else next(rest, None)
    if x is None or y is None:
        return None
    return x, y


def axis_values(spec: ParameterSpec, grid_size: int, tuned: Any) -> list[Any]:
    """``grid_size`` points over ``spec`` (its choices for a categorical or
    bool), with the tuned value added to a numeric axis."""
    [(_, values)] = grid_axes([spec], grid_size)
    if spec.kind in _NUMERIC and tuned is not None:
        values = sorted({*values, tuned})
    return list(values)


def sweep_heatmap(
    strategy_cls: type[Strategy],
    space: ParamSpace,
    tuned: Mapping[str, Any],
    dataset: Any,
    objective: Objective,
    options: HeatmapOptions,
    *,
    pinned: Collection[str] = (),
    parallel: ParallelSettings | None = None,
    seed: int = 0,
) -> tuple[ParameterHeatmap, list[TrialOutcome]]:
    """The heatmap around ``tuned`` and one trial per cell, row by row (see
    the module doc). ``ValueError`` when the axes can't be picked."""
    axes = pick_axes(space, options, pinned)
    if axes is None:
        raise ValueError("a heatmap needs two tunable parameters")
    x_spec, y_spec = axes
    base = merge_with_defaults(dict(tuned), space)
    xs = axis_values(x_spec, options.grid_size, base.get(x_spec.name))
    ys = axis_values(y_spec, options.grid_size, base.get(y_spec.name))
    candidates = [{**base, x_spec.name: xv, y_spec.name: yv} for yv in ys for xv in xs]
    _log.info(
        "heatmap.start",
        strategy=strategy_cls.__name__,
        x=x_spec.name,
        y=y_spec.name,
        cells=len(candidates),
    )
    fast_scores = _fast_scores(strategy_cls, candidates, dataset) if options.fast else None
    if fast_scores is not None:
        trials = [
            TrialOutcome.failed(c, f"{HEATMAP_FAST} (fast score {s:.4g})")
            for c, s in zip(candidates, fast_scores, strict=True)
        ]
        flat, metric = fast_scores, FAST_METRIC
    else:
        trials = evaluate_candidates(
            strategy_cls,
            candidates,
            objective,
            dataset,
            parallel=parallel or ParallelSettings(),
            root_seed=seed,
            log_prefix="heatmap",
        )
        flat = [float(t.score) for t in trials]
        metric = str(getattr(objective, "name", type(objective).__name__))
    width = len(xs)
    heatmap = ParameterHeatmap(
        x=x_spec.name,
        y=y_spec.name,
        x_values=xs,
        y_values=ys,
        scores=[flat[i : i + width] for i in range(0, len(flat), width)],
        metric=metric,
        fast=fast_scores is not None,
        best={x_spec.name: base.get(x_spec.name), y_spec.name: base.get(y_spec.name)},
        fixed={k: v for k, v in base.items() if k not in (x_spec.name, y_spec.name)},
    )
    return heatmap, trials


def _fast_scores(
    strategy_cls: type[Any], candidates: Sequence[dict[str, Any]], dataset: Any
) -> list[float] | None:
    """Fast scores of every candidate on the train window; ``None`` when the
    strategy has no fast path, there is no lake, or no cell scores."""
    from stonks.lab.vectorized import load_closes, prescreen, supports_vectorized

    lake = getattr(dataset, "lake", None)
    window = getattr(dataset, "train_window", None)
    if not supports_vectorized(strategy_cls) or lake is None or window is None:
        return None
    closes = load_closes(lake, list(getattr(dataset, "universe", []) or []), window[1])
    if closes.empty:
        return None
    screened = prescreen(strategy_cls, candidates, closes, start=window[0], cost_bps=_FAST_COST_BPS)
    scores = [float(s.score) for s in screened]
    if not any(math.isfinite(s) for s in scores):
        _log.info("heatmap.fast.unscored", strategy=strategy_cls.__name__)
        return None
    return scores


def plateau_overlay(
    heatmap: ParameterHeatmap,
    report: SurvivalReport | None,
    space: ParamSpace,
    *,
    step: float,
) -> ParameterHeatmap:
    """``heatmap`` with the plateau ``report``'s verdict and neighbourhood;
    unchanged when there is no report."""
    if report is None:
        return heatmap
    by_name = {s.name: s for s in space}

    def neighbourhood(name: str) -> tuple[float, float] | None:
        spec = by_name.get(name)
        centre = heatmap.best.get(name)
        if spec is None or spec.kind not in _NUMERIC or centre is None or not spec.bounds:
            return None
        lo, hi = (float(b) for b in spec.bounds)
        delta = step * (hi - lo)
        return max(lo, float(centre) - delta), min(hi, float(centre) + delta)

    overlay = PlateauOverlay(
        passed=bool(report.passed),
        notes=str(report.notes),
        step=float(step),
        x_range=neighbourhood(heatmap.x),
        y_range=neighbourhood(heatmap.y),
        metrics={k: float(v) for k, v in dict(report.metrics).items() if math.isfinite(float(v))},
    )
    return dataclasses.replace(heatmap, plateau=overlay)
