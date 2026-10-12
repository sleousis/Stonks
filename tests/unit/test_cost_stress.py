"""Cost stress and cost budget (BL-18): unit-level pieces."""

from __future__ import annotations

import pytest

from stonks.backtest.costs import AssetClassCosts, CostModelSettings, Trade
from stonks.lab.survival.cost_stress import (
    CostStressOptions,
    CostStressTest,
    _has_costs,
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


_SALE = Trade(ticker="X.US", side="sell", quantity=100.0, price=50.0, asset_class="equity")


def _ibkr_istar() -> CostModelSettings:
    return CostModelSettings.ibkr("tiered").model_copy(update={"impact_model": "istar"})


def test_p19_scale_costs_at_1x_keeps_commissions_and_impact_settings():
    base = _ibkr_istar()
    expected = base.build().cost(_SALE)
    assert expected.fee == pytest.approx(0.8291, abs=1e-4)
    one = scale_costs(base, 1.0)
    assert one.build().cost(_SALE) == expected
    assert one.impact_model == "istar"
    assert one.for_asset_class("equity").commission == "ibkr_tiered"
    assert one.for_asset_class("equity").us_sell_fees


def test_p19_scale_costs_never_cheaper_at_2x():
    base = _ibkr_istar()
    one = scale_costs(base, 1.0).build().cost(_SALE)
    two = scale_costs(base, 2.0).build().cost(_SALE)
    assert two.fee >= one.fee
    assert two.fill_price <= one.fill_price  # a sale fills lower at higher costs
    # twice the fees, less a hair: the SEC fee is on the lower 2x fill value
    assert two.fee == pytest.approx(2 * one.fee, rel=1e-4)


def test_scaled_scales_impact_coefficients_and_commission_multiplier():
    base = _ibkr_istar().model_copy(update={"impact_gamma": 1.5})
    s = base.scaled(3.0)
    assert s.impact_gamma == pytest.approx(4.5)
    assert s.istar.a1 == pytest.approx(base.istar.a1 * 3.0)
    assert s.istar.a2 == base.istar.a2
    assert s.commissions.commission_multiplier == pytest.approx(3.0)
    assert base.commissions.commission_multiplier == 1.0
    zero = base.scaled(0.0)
    assert zero.build().cost(_SALE).fee == 0.0
    assert zero.build().cost(_SALE).fill_price == 50.0
    assert zero.charges_nothing()


def test_commission_only_settings_are_not_free():
    commission_only = CostModelSettings(
        default=AssetClassCosts(commission="ibkr_fixed"), max_impact_bps=0.0
    )
    assert not commission_only.charges_nothing()
    assert _has_costs(commission_only)
    fees_only = CostModelSettings(default=AssetClassCosts(us_sell_fees=True))
    assert not fees_only.charges_nothing()
    assert CostModelSettings().charges_nothing()
    assert not _has_costs(CostModelSettings())
    assert not _has_costs(None)


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
