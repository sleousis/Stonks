"""Cost stress and cost budget (BL-18): unit-level pieces."""

from __future__ import annotations

import pytest

from stonks.backtest.costs import AssetClassCosts, CostModelSettings
from stonks.lab.survival.cost_stress import (
    CostStressOptions,
    CostStressTest,
    break_even_multiple,
    scale_costs,
)
from stonks.lab.survival.registry import build_survival_test, survival_test_names


def test_registered_under_its_id():
    assert "cost_stress" in survival_test_names()
    test = build_survival_test("cost_stress", {"max_workers": 1})
    assert isinstance(test, CostStressTest)


def test_defaults_follow_the_spec():
    o = CostStressOptions()
    assert o.multipliers == (0.0, 1.0, 2.0, 3.0)
    assert o.stress_multiplier == 2.0
    assert o.min_stressed_sharpe_fraction == 0.5
    assert o.min_break_even == 2.0
    assert o.max_break_even == 10.0
    assert o.max_cost_sharpe == 0.13
    assert o.max_cost_sharpe_fraction == pytest.approx(1 / 3)


def test_multipliers_must_include_zero_one_and_the_stress_level():
    with pytest.raises(ValueError):
        CostStressOptions(multipliers=(1.0, 2.0))
    with pytest.raises(ValueError):
        CostStressOptions(multipliers=(0.0, 1.0, 3.0), stress_multiplier=2.0)


def test_scale_costs_scales_every_cost_field():
    base = CostModelSettings(
        default=AssetClassCosts(fee_flat=1.0, fee_bps=2.0, half_spread_bps=3.0),
        asset_classes={"crypto": AssetClassCosts(fee_bps=10.0, half_spread_bps=5.0)},
        impact_bps=100.0,
        max_impact_bps=500.0,
    )
    s = scale_costs(base, 3.0)
    assert s.default == AssetClassCosts(fee_flat=3.0, fee_bps=6.0, half_spread_bps=9.0)
    assert s.asset_classes["crypto"] == AssetClassCosts(fee_bps=30.0, half_spread_bps=15.0)
    assert s.impact_bps == 300.0
    assert s.max_impact_bps == 1500.0
    zero = scale_costs(base, 0.0)
    assert zero.default == AssetClassCosts()
    assert zero.impact_bps == 0.0
    assert base.impact_bps == 100.0  # the input is never edited


def test_scale_costs_keeps_sell_prices_positive_at_extreme_multiples():
    base = CostModelSettings(
        default=AssetClassCosts(half_spread_bps=50.0), impact_bps=100.0, max_impact_bps=2000.0
    )
    s = scale_costs(base, 10.0)
    assert s.default.half_spread_bps + s.max_impact_bps < 10_000


def _sharpe_line(zero: float, slope: float):
    return lambda m: zero - slope * m


def test_break_even_bisects_between_grid_points():
    sharpe = _sharpe_line(1.0, 0.4)  # crosses zero at m = 2.5
    grid = {m: sharpe(m) for m in (0.0, 1.0, 2.0, 3.0)}
    got = break_even_multiple(grid, sharpe, upper=10.0, tolerance=0.01)
    assert got == pytest.approx(2.5, abs=0.01)


def test_break_even_is_zero_without_a_pre_cost_edge_and_capped_above():
    assert break_even_multiple({0.0: -0.1, 1.0: -0.2}, lambda m: -1.0, upper=10.0) == 0.0
    assert break_even_multiple({0.0: 1.0, 1.0: 0.9}, lambda m: 0.5, upper=10.0) == 10.0


def test_break_even_falls_as_costs_bite_harder():
    a = break_even_multiple({0.0: 1.0}, _sharpe_line(1.0, 0.2), upper=10.0, tolerance=0.01)
    b = break_even_multiple({0.0: 1.0}, _sharpe_line(1.0, 0.4), upper=10.0, tolerance=0.01)
    assert b < a
