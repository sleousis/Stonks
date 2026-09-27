"""The lab hands its dataset's cost model to every strategy it builds
(roadmap 22.10). A cost stress rerun keeps the base costs: the strategy
believes the configured costs while the broker charges the stressed ones."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from stonks.backtest.costs import AssetClassCosts, CostModelSettings
from stonks.lab.survival import _reruns
from stonks.lab.tuning.base import fitted_strategy
from stonks.strategies.base import BaseStrategy

BASE = CostModelSettings(default=AssetClassCosts(half_spread_bps=3.0))
STRESSED = CostModelSettings(default=AssetClassCosts(half_spread_bps=9.0))


class _Aware(BaseStrategy):
    id = "cost_aware_probe"

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        self.bound: CostModelSettings | None = None
        self.fitted_with: CostModelSettings | None = None

    @classmethod
    def parameter_spec(cls):
        return []

    def bind_costs(self, costs: CostModelSettings) -> None:
        self.bound = costs

    def fit(self, dataset: Any) -> None:
        self.fitted_with = self.bound

    def estimate_return(self, ticker, as_of, lake):
        return None


@dataclass
class _Dataset:
    costs: CostModelSettings | None = None


def test_fitted_strategy_binds_the_dataset_costs_before_the_fit():
    strategy = fitted_strategy(_Aware, {}, _Dataset(costs=BASE))
    assert isinstance(strategy, _Aware)
    assert strategy.bound == BASE and strategy.fitted_with == BASE


def test_fitted_strategy_takes_explicit_costs():
    strategy = fitted_strategy(_Aware, {}, _Dataset(costs=STRESSED), costs=BASE)
    assert isinstance(strategy, _Aware) and strategy.bound == BASE


def test_a_dataset_without_costs_binds_nothing():
    strategy = fitted_strategy(_Aware, {}, _Dataset())
    assert isinstance(strategy, _Aware) and strategy.bound is None


def test_a_rerun_strategy_keeps_the_base_costs():
    state = SimpleNamespace(saved=(_Aware, {}), dataset=_Dataset(costs=BASE))
    rerun = _reruns.Rerun(params={}, costs=STRESSED, override_costs=True)
    strategy = _reruns._strategy_for(state, rerun, _Dataset(costs=STRESSED))
    assert isinstance(strategy, _Aware) and strategy.bound == BASE
