"""A lab run's ``[lab.parallel] max_workers`` holds for every pool in the
run: the tuner, the heatmap and each survival test."""

from __future__ import annotations

from datetime import date
from typing import Literal

import pytest

from stonks.core.protocols import SurvivalReport
from stonks.lab import parallel
from stonks.lab.dataset import LabDataset
from stonks.lab.parallel import (
    ParallelSettings,
    planned_workers,
    resolve_max_workers,
    run_max_workers,
)
from stonks.lab.runner import LabRunner
from stonks.lab.survival.base import SurvivalSuite
from stonks.lab.tuning.grid import GridTuner
from tests.fixtures.parallel_lab import NoisyParamStrategy


def test_the_run_cap_is_the_default_before_the_env_and_the_cpu_count(monkeypatch):
    monkeypatch.setattr(parallel, "default_max_workers", lambda: 8)
    assert resolve_max_workers(None) == 8
    with run_max_workers(2):
        assert resolve_max_workers(None) == 2
        assert ParallelSettings().resolved_workers() == 2
    assert resolve_max_workers(None) == 8


def test_the_run_cap_bounds_an_explicit_worker_count(monkeypatch):
    with run_max_workers(1):
        assert planned_workers(10, max_workers=4) == 1
        assert planned_workers(10, settings=ParallelSettings(max_workers=6)) == 1
    assert planned_workers(10, max_workers=4) == 4


def test_no_cap_leaves_explicit_counts_alone():
    with run_max_workers(None):
        assert planned_workers(10, max_workers=3) == 3


class _AB:
    name = "ab"
    direction: Literal["maximize", "minimize"] = "maximize"

    def score(self, strategy, dataset) -> float:
        p = strategy.params
        return float(p["a"]) + p["b"]


def _square(state, task):
    return task * task


class _PoolingTest:
    """A survival test that fans out with the default worker count, as
    every registered test does when its options leave ``max_workers`` unset."""

    id = "pooling"

    def run(self, strategy, context) -> SurvivalReport:
        out = parallel.run_tasks(_square, list(range(6)))
        return SurvivalReport(self.id, out == [i * i for i in range(6)], {}, "")


class _NoPool:
    def __init__(self, *a, **k):
        raise AssertionError("a process pool started")


def test_a_run_with_one_worker_starts_no_pool_anywhere(monkeypatch):
    monkeypatch.setattr(parallel, "default_max_workers", lambda: 4)
    monkeypatch.setattr(parallel, "ProcessPoolExecutor", _NoPool)
    ds = LabDataset(lake=None, universe=["X.US"], start=date(2024, 1, 1), end=date(2024, 6, 1))
    runner = LabRunner(
        GridTuner(grid_size=2),  # its own default: every core
        _AB(),
        SurvivalSuite([_PoolingTest()]),
        budget=4,
        preflight=False,
        parallel=ParallelSettings(max_workers=1),
    )
    result = runner.run(NoisyParamStrategy, ds)
    assert [r.passed for r in result.survival_reports] == [True]
    # the cap ends with the run
    assert resolve_max_workers(None) == 4


def test_without_the_cap_the_same_test_would_start_a_pool(monkeypatch):
    monkeypatch.setattr(parallel, "default_max_workers", lambda: 4)
    monkeypatch.setattr(parallel, "ProcessPoolExecutor", _NoPool)
    with pytest.raises(AssertionError, match="pool started"):
        _PoolingTest().run(None, None)
