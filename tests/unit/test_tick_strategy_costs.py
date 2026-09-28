"""The tick hands its cost model to every strategy it loads (roadmap
22.10): the ranker's scoring instances and the pool's late loads."""

from __future__ import annotations

from typing import Any

import pytest

from stonks.backtest.costs import AssetClassCosts, CostModelSettings
from stonks.production.ranker import Ranker, StrategyPool
from stonks.strategies.base import BaseStrategy
from tests.unit.trend_helpers import LAST, build_lake, trend

COSTS = CostModelSettings(default=AssetClassCosts(half_spread_bps=4.0))


class _Aware(BaseStrategy):
    id = "cost_aware_probe"

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        self.bound: CostModelSettings | None = None

    @classmethod
    def parameter_spec(cls):
        return []

    def bind_costs(self, costs: CostModelSettings) -> None:
        self.bound = costs

    def estimate_return(self, ticker, as_of, lake):
        return None


class _Registry:
    def __init__(self) -> None:
        self.loaded: list[_Aware] = []

    def load(self, strategy_id: str) -> _Aware:
        strategy = _Aware({})
        self.loaded.append(strategy)
        return strategy


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    db = build_lake(tmp_path_factory.mktemp("tick_costs") / "lake.duckdb", {"UP.US": trend(0.001)})
    yield db
    db.close()


def test_the_ranker_binds_its_costs(lake):
    probe = _Aware({})
    signals = Ranker(
        registry=_Registry(),  # type: ignore[arg-type]
        lake=lake,
        universe=["UP.US"],
        loaders={"probe": lambda: probe},
        costs=COSTS,
    ).score(as_of=LAST.date())
    assert signals.instances["probe"] is probe
    assert probe.bound == COSTS


def test_the_ranker_without_costs_binds_nothing(lake):
    probe = _Aware({})
    Ranker(
        registry=_Registry(),  # type: ignore[arg-type]
        lake=lake,
        universe=["UP.US"],
        loaders={"probe": lambda: probe},
    ).score(as_of=LAST.date())
    assert probe.bound is None


def test_the_pool_binds_what_it_loads(lake):
    registry = _Registry()
    pool = StrategyPool(registry, lake, costs=COSTS)  # type: ignore[arg-type]
    strategy = pool.checkout("late")
    assert isinstance(strategy, _Aware) and strategy.bound == COSTS
