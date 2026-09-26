"""Signal research on the process pool (BL-33 to BL-35): results do not
depend on the worker count."""

from __future__ import annotations

import pytest

from stonks.lab.objectives import SharpeObjective
from stonks.lab.parallel import ParallelSettings
from stonks.lab.signal_eval import signal_ic
from stonks.lab.survival.base import TuningSetup
from stonks.lab.survival.event_study import event_study
from stonks.lab.survival.registry import build_survival_test
from stonks.lab.tuning.grid import GridTuner
from tests.fixtures.signal_research import (
    FutureReturnSignal,
    NoiseSignal,
    WeekdaySignal,
    dataset_for,
    signal_lake,
)

TICKERS = [f"T{i:02d}" for i in range(10)]


@pytest.fixture(scope="module")
def lake():
    lake = signal_lake(":memory:", TICKERS, periods=160, seed=5)
    yield lake
    lake.close()


def test_signal_ic_same_for_any_worker_count(lake):
    ds = dataset_for(lake, TICKERS)
    serial = signal_ic(NoiseSignal({"seed": 2}), ds, (1, 5), every_bars=2, max_workers=1)
    pooled = signal_ic(NoiseSignal({"seed": 2}), ds, (1, 5), every_bars=2, max_workers=2)
    assert serial.to_dict() == pooled.to_dict()


def test_event_study_same_for_any_worker_count(lake):
    ds = dataset_for(lake, TICKERS[:4])
    strategy = FutureReturnSignal({"horizon": 5, "threshold": 0.01})
    serial = event_study(strategy, ds, ds.full_window, holding_bars=5, max_workers=1)
    pooled = event_study(strategy, ds, ds.full_window, holding_bars=5, max_workers=3)
    assert serial.to_dict() == pooled.to_dict()


def test_vs_random_same_for_any_worker_count(lake):
    ds = dataset_for(lake, TICKERS[:2])
    setup = TuningSetup(
        tuner=GridTuner(grid_size=5, parallel=ParallelSettings(max_workers=1)),
        objective=SharpeObjective(),
        budget=3,
    )
    reports = []
    for workers in (1, 3):
        test = build_survival_test("vs_random", {"k": 5, "max_workers": workers})
        test.bind_tuning(setup)
        reports.append(test.run(WeekdaySignal({"weekday": 1}), ds))
    assert reports[0] == reports[1]
