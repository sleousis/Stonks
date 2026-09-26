"""BL-07: GridTuner / RandomTuner evaluate their trials through the
lab.parallel pool. Output must not depend on the worker count, failures
stay NaN trials, and TunerResult.trials lines up with history."""

from __future__ import annotations

import math
import os
from datetime import timedelta
from typing import Literal

import pytest

from stonks.core.protocols import TrialOutcome
from stonks.lab import parallel
from stonks.lab.dataset import LabDataset
from stonks.lab.objectives import SharpeObjective
from stonks.lab.parallel import ParallelSettings
from stonks.lab.survival.base import TuningSetup
from stonks.lab.tuning.base import fix_params, tune_and_fit
from stonks.lab.tuning.grid import GridTuner
from stonks.lab.tuning.random import RandomTuner
from stonks.strategies.examples.momentum import Momentum
from tests.fixtures.parallel_lab import (
    GlobalRngObjective,
    NoisyParamStrategy,
    lake_dates,
    random_walk_lake,
)


@pytest.fixture
def dataset(tmp_path):
    lake = random_walk_lake(tmp_path / "lake.duckdb", ["A.US", "B.US", "C.US"], periods=160)
    start, end = lake_dates(lake)
    yield LabDataset(
        lake=lake, universe=["A.US", "B.US", "C.US"], start=start, end=end, train_ratio=0.7
    )
    lake.close()


def _tuners(workers: int):
    settings = ParallelSettings(max_workers=workers)
    return [
        GridTuner(grid_size=3, seed=4, parallel=settings),
        RandomTuner(seed=9, parallel=settings),
    ]


def _tune(tuner, dataset, budget=6, objective=None, space=None, cls=Momentum):
    return tuner.tune(
        strategy_cls=cls,
        param_space=space if space is not None else cls.parameter_spec(),
        objective=objective or SharpeObjective(),
        dataset=dataset,
        budget=budget,
    )


def _same_history(a, b) -> bool:
    return len(a) == len(b) and all(
        pa == pb and (sa == sb or (math.isnan(sa) and math.isnan(sb)))
        for (pa, sa), (pb, sb) in zip(a, b, strict=True)
    )


@pytest.mark.parametrize("index", [0, 1], ids=["grid", "random"])
def test_results_are_identical_for_one_two_and_three_workers(dataset, index):
    results = [_tune(_tuners(w)[index], dataset) for w in (1, 2, 3)]
    serial = results[0]
    assert serial.trials is not None and len(serial.trials) == 6
    assert all(t.n_bars > 0 for t in serial.trials)
    for other in results[1:]:
        assert _same_history(other.history, serial.history)
        assert other.trials == serial.trials
        assert other.best_params == serial.best_params
        assert other.best_score == serial.best_score


def test_trials_align_with_history(dataset):
    result = _tune(RandomTuner(seed=2, parallel=ParallelSettings(max_workers=2)), dataset)
    assert _same_history([(t.params, t.score) for t in result.trials], result.history)
    assert all(isinstance(t, TrialOutcome) for t in result.trials)


def test_global_rng_use_is_reproducible_across_worker_counts():
    space = NoisyParamStrategy.parameter_spec()
    scores = [
        [
            s
            for _, s in _tune(
                t,
                "ds",
                budget=5,
                objective=GlobalRngObjective(),
                space=space,
                cls=NoisyParamStrategy,
            ).history
        ]
        for t in (RandomTuner(seed=3, parallel=ParallelSettings(max_workers=w)) for w in (1, 3))
    ]
    assert scores[0] == scores[1]


@pytest.mark.parametrize("workers", [1, 2])
def test_a_raising_trial_becomes_a_nan_trial(workers):
    space = fix_params(NoisyParamStrategy.parameter_spec(), {"fail_on": 0})
    tuner = GridTuner(grid_size=2, parallel=ParallelSettings(max_workers=workers))
    result = _tune(
        tuner, "ds", budget=20, objective=GlobalRngObjective(), space=space, cls=NoisyParamStrategy
    )
    failed = [t for t in result.trials if t.status == "failed"]
    assert failed and all(t.params["a"] == 0 and math.isnan(t.score) for t in failed)
    assert "a=0 fails" in failed[0].error
    assert all(math.isnan(s) for p, s in result.history if p["a"] == 0)
    assert result.best_params["a"] != 0


def test_one_worker_never_starts_a_pool_or_builds_a_snapshot(dataset, monkeypatch):
    def _forbidden(*a, **kw):
        raise AssertionError("pool-only work on the serial path")

    monkeypatch.setattr(parallel, "ProcessPoolExecutor", _forbidden)
    monkeypatch.setattr(parallel.LakeSnapshot, "build", _forbidden)
    for tuner in _tuners(1):
        assert _tune(tuner, dataset, budget=3).trials


def test_default_settings_follow_default_max_workers(dataset, monkeypatch):
    # conftest pins default_max_workers to 1, so the default tuner is serial
    monkeypatch.setattr(parallel, "ProcessPoolExecutor", _raise)
    assert _tune(GridTuner(grid_size=2), dataset, budget=2).trials


def _raise(*a, **kw):
    raise AssertionError("pool started")


class _UnpicklableObjective(SharpeObjective):
    def __init__(self) -> None:
        self.hook = lambda: None  # lambdas do not pickle


def test_unpicklable_objective_falls_back_to_serial(dataset, monkeypatch):
    serial = _tune(GridTuner(grid_size=2, parallel=ParallelSettings(max_workers=1)), dataset)
    monkeypatch.setattr(parallel, "ProcessPoolExecutor", _raise)
    fallback = _tune(
        GridTuner(grid_size=2, parallel=ParallelSettings(max_workers=4)),
        dataset,
        objective=_UnpicklableObjective(),
    )
    assert fallback.trials == serial.trials


class _CheckpointedObjective:
    """An objective wrapper exposing the cancellation hooks the tuners use
    on the pool path: ``worker_objective`` (what the workers run) and
    ``checkpoint`` (called in the parent between trials)."""

    name = "sharpe"
    direction: Literal["maximize", "minimize"] = "maximize"

    def __init__(self) -> None:
        self.worker_objective = SharpeObjective()
        self.calls: list[int] = []
        self._unpicklable = lambda: None

    def checkpoint(self) -> None:
        self.calls.append(os.getpid())

    def score(self, strategy, dataset):  # pragma: no cover - serial path only
        self.checkpoint()
        return self.worker_objective.score(strategy, dataset)


def test_pool_path_uses_worker_objective_and_checkpoints_in_the_parent(dataset):
    objective = _CheckpointedObjective()
    result = _tune(
        GridTuner(grid_size=2, parallel=ParallelSettings(max_workers=2)),
        dataset,
        budget=4,
        objective=objective,
    )
    assert len(result.trials) == 4 and all(t.n_bars > 0 for t in result.trials)
    assert objective.calls and set(objective.calls) == {os.getpid()}


def test_tune_and_fit_keeps_the_trials(dataset):
    setup = TuningSetup(
        tuner=GridTuner(grid_size=2, parallel=ParallelSettings(max_workers=2)),
        objective=SharpeObjective(),
        budget=4,
    )
    _, tuned = tune_and_fit(Momentum, dataset, setup, {"allocation": 0.5})
    assert tuned.best_params["allocation"] == 0.5
    assert tuned.trials is not None and len(tuned.trials) == len(tuned.history)


def test_workers_see_only_bars_up_to_the_dataset_end(dataset):
    # Bars after dataset.end are left out of the snapshot; results match
    # the serial run on the full lake.
    short = LabDataset(
        lake=dataset.lake,
        universe=dataset.universe,
        start=dataset.start,
        end=dataset.end - timedelta(days=30),
        train_ratio=0.7,
    )
    serial = _tune(RandomTuner(seed=1, parallel=ParallelSettings(max_workers=1)), short, budget=4)
    pooled = _tune(RandomTuner(seed=1, parallel=ParallelSettings(max_workers=2)), short, budget=4)
    assert pooled.trials == serial.trials
