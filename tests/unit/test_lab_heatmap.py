"""Parameter heatmaps (22.5, ``lab/heatmap.py``): a 2D sweep of any two
parameters around the tuned set, every cell a counted trial (P2), with the
plateau test's verdict laid over it (P4)."""

from __future__ import annotations

import math
from typing import Literal

import pytest

from stonks.core.params import ParameterSpec
from stonks.core.protocols import SurvivalReport
from stonks.lab.dataset import LabDataset
from stonks.lab.heatmap import (
    HEATMAP_FAST,
    HeatmapOptions,
    ParameterHeatmap,
    pick_axes,
    plateau_overlay,
    sweep_heatmap,
)
from stonks.lab.parallel import ParallelSettings
from stonks.strategies.examples.momentum import Momentum
from tests.fixtures.parallel_lab import NoisyParamStrategy, lake_dates, random_walk_lake

_SERIAL = ParallelSettings(max_workers=1)


class _AB:
    name = "ab"
    direction: Literal["maximize", "minimize"] = "maximize"

    def score(self, strategy, dataset) -> float:
        p = strategy.params
        return float(p["a"]) + p["b"]


def test_pick_axes_defaults_to_the_first_two_numeric_tunables():
    space = NoisyParamStrategy.parameter_spec()
    x, y = pick_axes(space, HeatmapOptions())
    assert (x.name, y.name) == ("a", "b")


def test_pick_axes_takes_named_parameters_and_refuses_bad_ones():
    space = NoisyParamStrategy.parameter_spec()
    x, y = pick_axes(space, HeatmapOptions(x="b", y="a"))
    assert (x.name, y.name) == ("b", "a")
    with pytest.raises(ValueError, match="unknown"):
        pick_axes(space, HeatmapOptions(x="zzz", y="a"))
    with pytest.raises(ValueError, match="not tunable"):
        pick_axes(space, HeatmapOptions(x="fail_on", y="a"))
    with pytest.raises(ValueError, match="pinned"):
        pick_axes(space, HeatmapOptions(x="a", y="b"), pinned={"a"})
    with pytest.raises(ValueError, match="two different"):
        pick_axes(space, HeatmapOptions(x="a", y="a"))


def test_pick_axes_is_none_with_fewer_than_two_tunables():
    space = [ParameterSpec(name="a", kind="int", default=1, bounds=(0, 3))]
    assert pick_axes(space, HeatmapOptions()) is None


def test_a_full_sweep_scores_every_cell_and_returns_every_trial():
    space = NoisyParamStrategy.parameter_spec()
    tuned = {"a": 5, "b": 0.5, "fail_on": -1}
    heatmap, trials = sweep_heatmap(
        NoisyParamStrategy,
        space,
        tuned,
        None,
        _AB(),
        HeatmapOptions(grid_size=3, fast=False),
        parallel=_SERIAL,
    )
    # the tuned value joins each axis so its cell is on the map
    assert heatmap.x_values == [0, 4, 5, 9]
    assert heatmap.y_values == [0.0, 0.5, 1.0]
    assert len(trials) == 12 == sum(len(row) for row in heatmap.scores)
    assert heatmap.scores[1][2] == pytest.approx(5.5)  # y=0.5, x=5
    assert heatmap.best == {"a": 5, "b": 0.5}
    assert heatmap.metric == "ab" and heatmap.fast is False
    assert all(t.status == "ok" for t in trials)
    assert all(t.params["fail_on"] == -1 for t in trials)


def test_a_failed_cell_is_nan_and_still_a_trial():
    space = NoisyParamStrategy.parameter_spec()
    tuned = {"a": 4, "b": 0.5, "fail_on": 9}
    heatmap, trials = sweep_heatmap(
        NoisyParamStrategy,
        space,
        tuned,
        None,
        _AB(),
        HeatmapOptions(grid_size=2, fast=False),
        parallel=_SERIAL,
    )
    failed = [t for t in trials if t.status == "failed"]
    assert failed and all(t.params["a"] == 9 for t in failed)
    col = heatmap.x_values.index(9)
    assert all(math.isnan(row[col]) for row in heatmap.scores)


class _TwoAxis(Momentum):
    @classmethod
    def parameter_spec(cls):
        spec = {s.name: s for s in super().parameter_spec()}
        spec["lookback_days"] = ParameterSpec(
            name="lookback_days", kind="int", default=20, bounds=(10, 60)
        )
        return list(spec.values())


@pytest.fixture
def dataset(tmp_path):
    lake = random_walk_lake(tmp_path / "lake.duckdb", ["A.US", "B.US"], periods=200)
    start, end = lake_dates(lake)
    yield LabDataset(lake=lake, universe=["A.US", "B.US"], start=start, end=end)
    lake.close()


def test_the_fast_path_scores_cells_cheaply_and_counts_them_as_screened_trials(dataset):
    class _NeverCalled:
        name = "never"
        direction: Literal["maximize", "minimize"] = "maximize"

        def score(self, strategy, dataset):  # pragma: no cover - must not run
            raise AssertionError("the fast path must not run full backtests")

    space = _TwoAxis.parameter_spec()
    tuned = {s.name: s.default for s in space}
    heatmap, trials = sweep_heatmap(
        _TwoAxis,
        space,
        tuned,
        dataset,
        _NeverCalled(),
        HeatmapOptions(grid_size=4),
        parallel=_SERIAL,
    )
    assert heatmap.fast is True and heatmap.metric == "fast_sharpe"
    cells = len(heatmap.x_values) * len(heatmap.y_values)
    assert len(trials) == cells
    assert all(t.status == "failed" and t.error.startswith(HEATMAP_FAST) for t in trials)
    assert any(math.isfinite(v) for row in heatmap.scores for v in row)


def test_the_fast_path_falls_back_to_full_backtests_when_it_scores_nothing(dataset):
    space = _TwoAxis.parameter_spec()
    tuned = {s.name: s.default for s in space}
    empty = LabDataset(
        lake=dataset.lake, universe=["NOPE.US"], start=dataset.start, end=dataset.end
    )

    class _One:
        name = "one"
        direction: Literal["maximize", "minimize"] = "maximize"

        def score(self, strategy, dataset):
            return 1.0

    heatmap, trials = sweep_heatmap(
        _TwoAxis, space, tuned, empty, _One(), HeatmapOptions(grid_size=2), parallel=_SERIAL
    )
    assert heatmap.fast is False
    assert all(t.status == "ok" for t in trials)


def _map() -> ParameterHeatmap:
    return ParameterHeatmap(
        x="a",
        y="b",
        x_values=[0, 5, 10],
        y_values=[0.0, 0.5, 1.0],
        scores=[[1.0, 2.0, float("nan")], [3.0, 4.0, 5.0], [6.0, 7.0, 8.0]],
        metric="sharpe",
        fast=False,
        best={"a": 5, "b": 0.5},
        fixed={"c": 1},
    )


def test_the_plateau_verdict_and_its_neighbourhood_are_laid_over_the_map():
    space = [
        ParameterSpec(name="a", kind="int", default=5, bounds=(0, 10)),
        ParameterSpec(name="b", kind="float", default=0.5, bounds=(0.0, 1.0)),
    ]
    report = SurvivalReport("plateau", True, {"train_ratio": 0.9}, "tuned params sit on a plateau")
    overlaid = plateau_overlay(_map(), report, space, step=0.2)
    assert overlaid.plateau is not None
    assert overlaid.plateau.passed is True
    assert overlaid.plateau.x_range == (3.0, 7.0)
    assert overlaid.plateau.y_range == pytest.approx((0.3, 0.7))
    assert overlaid.plateau.metrics == {"train_ratio": 0.9}
    assert plateau_overlay(_map(), None, space, step=0.2).plateau is None


def test_a_heatmap_round_trips_through_json():
    import json

    heatmap = plateau_overlay(
        _map(),
        SurvivalReport("plateau", False, {}, "train ratio 0.2 < 0.7"),
        [
            ParameterSpec(name="a", kind="int", default=5, bounds=(0, 10)),
            ParameterSpec(name="b", kind="categorical", default=0.5, bounds=[0.0, 0.5, 1.0]),
        ],
        step=0.15,
    )
    data = json.loads(json.dumps(heatmap.to_dict()))
    assert data["scores"][0][2] is None  # NaN is not JSON
    again = ParameterHeatmap.from_dict(data)
    assert again.plateau is not None and again.plateau.y_range is None
    assert math.isnan(again.scores[0][2])
    assert again.to_dict() == heatmap.to_dict()


def test_options_are_validated():
    with pytest.raises(ValueError):
        HeatmapOptions(grid_size=1)
    with pytest.raises(ValueError):
        HeatmapOptions(grid_size=40)
