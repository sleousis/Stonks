"""The lab-run code path draws the heatmap (22.5) with the Optuna tuner
(22.1): the view carries the map and the plateau verdict, every cell is a
trial, and a bad axis fails before any work."""

from __future__ import annotations

import json
from datetime import date

import pytest

from stonks.app.errors import ValidationError
from stonks.app.lab import LabRunRequest, execute_lab_run
from stonks.app.strategies import StrategyRef
from stonks.app.sweep import SweepRequest
from stonks.config import Settings
from stonks.lab.parallel import ParallelSettings
from stonks.strategies.examples.momentum import Momentum

MOMENTUM = "stonks.strategies.examples.momentum:Momentum"


def _request(**overrides) -> LabRunRequest:
    body = {
        "strategy": StrategyRef(class_path=MOMENTUM),
        "universe": ["UP.US", "FLAT.US", "DOWN.US"],
        "start": date(2025, 10, 1),
        "end": date(2026, 4, 1),
        "tuner": "optuna",
        "budget": 4,
        "survival_tests": ["plateau"],
        "cost_model": "zero",
        "preflight": False,
        "heatmap": {"x": "lookback_days", "y": "threshold", "grid_size": 3},
    }
    body.update(overrides)
    return LabRunRequest(**body)


def test_the_view_carries_the_heatmap_and_the_plateau_verdict(lake_trending):
    execution = execute_lab_run(
        Settings(),
        Momentum,
        _request(),
        lake=lake_trending,
        parallel=ParallelSettings(max_workers=1),
    )
    view = execution.view()
    heatmap = view.heatmap
    assert heatmap is not None
    assert (heatmap.x, heatmap.y) == ("lookback_days", "threshold")
    assert heatmap.fast is True and heatmap.metric == "fast_sharpe"
    cells = len(heatmap.x_values) * len(heatmap.y_values)
    assert view.n_trials_run == 4 + cells
    [plateau] = [r for r in view.survival_reports if r.test_id == "plateau"]
    assert heatmap.plateau is not None and heatmap.plateau.passed == plateau.passed
    json.dumps(view.model_dump(mode="json"))  # no NaN in the payload


def test_full_cells_use_the_objective(lake_trending):
    execution = execute_lab_run(
        Settings(),
        Momentum,
        _request(
            tuner="random", budget=2, objective="sortino", heatmap={"grid_size": 2, "fast": False}
        ),
        lake=lake_trending,
        parallel=ParallelSettings(max_workers=1),
    )
    heatmap = execution.view().heatmap
    assert heatmap is not None and heatmap.metric == "sortino" and heatmap.fast is False


def test_a_bad_axis_fails_before_any_work(lake_trending):
    with pytest.raises(ValidationError, match="heatmap"):
        execute_lab_run(Settings(), Momentum, _request(heatmap={"x": "nope"}), lake=lake_trending)


def test_a_multi_strategy_sweep_draws_no_heatmap():
    with pytest.raises(ValueError, match="heatmap"):
        SweepRequest(
            universe=["UP.US"],
            start=date(2025, 1, 1),
            end=date(2025, 6, 1),
            heatmap={"grid_size": 3},
        )
