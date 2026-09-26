"""The shared re-run helper behind cost stress, plateau and cross-instrument."""

from __future__ import annotations

import math

import pytest

from stonks.backtest.costs import CostModelSettings
from stonks.lab.survival._reruns import Rerun, run_reruns
from tests.fixtures.robustness_lab import (
    FlipFlop,
    SurfaceObjective,
    SurfaceStrategy,
    dataset_for,
    trend_lake,
)


@pytest.fixture
def ds():
    lake = trend_lake(":memory:", {"A.US": (0.002, 0.01), "B.US": (-0.002, 0.01)})
    yield dataset_for(lake, ["A.US", "B.US"])
    lake.close()


def test_results_come_back_in_order_and_start_from_the_same_state(ds):
    strategy = FlipFlop({"hold_bars": 3})
    strategy._bar = 7  # state from an earlier backtest must not leak into reruns
    a, b = run_reruns(strategy, ds, [Rerun(), Rerun()], max_workers=1)
    assert a == b
    assert a.n_trades > 0


def test_params_universe_and_costs_overrides(ds):
    strategy = FlipFlop({"hold_bars": 3})
    only_a, only_b, costly, slow = run_reruns(
        strategy,
        ds,
        [
            Rerun(universe=("A.US",)),
            Rerun(universe=("B.US",)),
            Rerun(costs=CostModelSettings.realistic(), override_costs=True),
            Rerun(params={"hold_bars": 20}),
        ],
        max_workers=1,
    )
    assert only_a.pnl > 0 > only_b.pnl
    assert costly.costs_paid > 0
    assert slow.n_trades < only_a.n_trades


def test_objective_score_needs_an_objective(ds):
    with pytest.raises(ValueError):
        run_reruns(SurfaceStrategy({}), ds, [Rerun(score_objective=True)])
    (r,) = run_reruns(
        SurfaceStrategy({}),
        ds,
        [Rerun(params={"a": 10, "b": 0.5}, score_objective=True)],
        objective=SurfaceObjective(),
    )
    assert r.objective_score == pytest.approx(1.0)
    assert r.n_round_trips == 2  # buys both tickers once and holds


def test_a_failing_rerun_becomes_an_error_result(ds):
    (r,) = run_reruns(FlipFlop({}), ds, [Rerun(params={"hold_bars": 0})], max_workers=1)
    assert not r.ok
    assert math.isnan(r.sharpe)
