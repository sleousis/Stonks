"""API views of a lab run's parameter heatmap (22.5, ``lab/heatmap.py``)."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from pydantic import BaseModel

from stonks.app.errors import ValidationError
from stonks.app.serialize import FiniteFloat, finite, to_jsonable
from stonks.lab.heatmap import HeatmapOptions, ParameterHeatmap, pick_axes

__all__ = ["HeatmapView", "PlateauOverlayView", "check_heatmap_axes"]


class PlateauOverlayView(BaseModel):
    """The plateau test's verdict on the tuned set, laid over the map."""

    passed: bool
    notes: str
    #: Fraction of each numeric range the plateau test nudges by.
    step: float
    #: ``[low, high]`` of the neighbourhood on each axis (``None`` when the
    #: axis is not numeric).
    x_range: list[float] | None = None
    y_range: list[float] | None = None
    metrics: dict[str, FiniteFloat] = {}


class HeatmapView(BaseModel):
    """Scores over ``y_values`` (rows) by ``x_values`` (columns), ``None``
    for a cell that failed. ``metric`` is the run's objective, or
    ``fast_sharpe`` when the cells were scored on the vectorised fast path.
    Every cell is a trial in the ledger."""

    x: str
    y: str
    x_values: list[Any]
    y_values: list[Any]
    scores: list[list[FiniteFloat]]
    metric: str
    fast: bool
    #: The tuned point (``{x: value, y: value}``).
    best: dict[str, Any]
    #: The other parameters the sweep held at their tuned values.
    fixed: dict[str, Any]
    plateau: PlateauOverlayView | None = None

    @classmethod
    def of(cls, heatmap: ParameterHeatmap) -> HeatmapView:
        overlay = heatmap.plateau
        return cls(
            x=heatmap.x,
            y=heatmap.y,
            x_values=to_jsonable(heatmap.x_values),
            y_values=to_jsonable(heatmap.y_values),
            scores=[[finite(v) for v in row] for row in heatmap.scores],
            metric=heatmap.metric,
            fast=heatmap.fast,
            best=to_jsonable(heatmap.best),
            fixed=to_jsonable(heatmap.fixed),
            plateau=(
                PlateauOverlayView(
                    passed=overlay.passed,
                    notes=overlay.notes,
                    step=overlay.step,
                    x_range=list(overlay.x_range) if overlay.x_range else None,
                    y_range=list(overlay.y_range) if overlay.y_range else None,
                    metrics={k: finite(v) for k, v in overlay.metrics.items()},
                )
                if overlay is not None
                else None
            ),
        )


def check_heatmap_axes(
    strategy_cls: type[Any], options: HeatmapOptions | None, pinned: Collection[str] = ()
) -> None:
    """``ValidationError`` when ``options`` name a parameter the strategy
    can't sweep, so a bad request fails before it is queued."""
    if options is None:
        return
    try:
        pick_axes(strategy_cls.parameter_spec(), options, pinned)
    except ValueError as exc:
        raise ValidationError(f"heatmap: {exc}") from None
