"""Unit tests for the backtest ``CostModel`` seam and its implementations."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from stonks.backtest.costs import (
    AssetClassCostModel,
    AssetClassCosts,
    CostModel,
    CostModelSettings,
    FixedCostModel,
    Trade,
)


def _trade(**kw) -> Trade:
    base = {"ticker": "AAPL.US", "side": "buy", "quantity": 10.0, "price": 100.0}
    base.update(kw)
    return Trade(**base)


# ---- FixedCostModel (the legacy slippage_bps + fee_per_trade behaviour) -----


def test_fixed_model_satisfies_protocol():
    assert isinstance(FixedCostModel(), CostModel)
    assert isinstance(CostModelSettings().build(), AssetClassCostModel)
    assert isinstance(CostModelSettings().build(), CostModel)


def test_fixed_model_is_free_by_default():
    cost = FixedCostModel().cost(_trade())
    assert cost.fill_price == 100.0
    assert cost.fee == 0.0


def test_fixed_model_applies_adverse_slippage_and_flat_fee():
    model = FixedCostModel(slippage_bps=50.0, fee_per_trade=1.5)
    buy = model.cost(_trade(side="buy"))
    sell = model.cost(_trade(side="sell"))
    assert buy.fill_price == pytest.approx(100.5)
    assert sell.fill_price == pytest.approx(99.5)
    assert buy.fee == sell.fee == 1.5


# ---- AssetClassCostModel ----------------------------------------------------


def _settings(**kw) -> CostModelSettings:
    return CostModelSettings(**kw)


def test_per_asset_class_fee_is_flat_plus_bps_of_notional():
    model = _settings(asset_classes={"crypto": AssetClassCosts(fee_flat=1.0, fee_bps=10.0)}).build()
    cost = model.cost(_trade(asset_class="crypto", quantity=10.0, price=100.0))
    assert cost.fill_price == pytest.approx(100.0)
    assert cost.fee == pytest.approx(1.0 + 0.001 * 1_000.0)


def test_half_spread_is_paid_in_the_adverse_direction():
    model = _settings(default=AssetClassCosts(half_spread_bps=5.0)).build()
    assert model.cost(_trade(side="buy")).fill_price == pytest.approx(100.05)
    assert model.cost(_trade(side="sell")).fill_price == pytest.approx(99.95)


def test_bps_fee_is_charged_on_the_fill_notional():
    model = _settings(default=AssetClassCosts(fee_bps=10.0, half_spread_bps=100.0)).build()
    cost = model.cost(_trade(quantity=10.0, price=100.0))
    assert cost.fee == pytest.approx(0.001 * 10.0 * 101.0)


def test_unknown_asset_class_uses_default_costs():
    model = _settings(
        default=AssetClassCosts(fee_flat=2.0),
        asset_classes={"crypto": AssetClassCosts(fee_flat=9.0)},
    ).build()
    assert model.cost(_trade(asset_class="bond")).fee == pytest.approx(2.0)


def test_sqrt_impact_grows_with_participation():
    model = _settings(impact_bps=100.0).build()
    # participation 1% -> 100 * sqrt(0.01) = 10 bps
    small = model.cost(_trade(quantity=10.0, bar_volume=1_000.0))
    assert small.fill_price == pytest.approx(100.0 * (1 + 10.0 / 10_000))
    # 4x the participation -> 2x the impact
    big = model.cost(_trade(quantity=40.0, bar_volume=1_000.0))
    assert (big.fill_price - 100.0) == pytest.approx(2 * (small.fill_price - 100.0))


def test_impact_adds_to_half_spread_and_is_adverse_for_sells():
    model = _settings(default=AssetClassCosts(half_spread_bps=5.0), impact_bps=100.0).build()
    cost = model.cost(_trade(side="sell", quantity=10.0, bar_volume=1_000.0))
    assert cost.fill_price == pytest.approx(100.0 * (1 - 15.0 / 10_000))


def test_impact_is_capped():
    model = _settings(impact_bps=100.0, max_impact_bps=50.0).build()
    cost = model.cost(_trade(quantity=1_000.0, bar_volume=10.0))  # 100x the bar
    assert cost.fill_price == pytest.approx(100.0 * 1.005)


def test_zero_volume_bar_pays_the_capped_impact():
    model = _settings(impact_bps=100.0, max_impact_bps=50.0).build()
    cost = model.cost(_trade(bar_volume=0.0))
    assert cost.fill_price == pytest.approx(100.0 * 1.005)


def test_unknown_volume_has_no_impact():
    model = _settings(impact_bps=100.0).build()
    assert model.cost(_trade(bar_volume=None)).fill_price == pytest.approx(100.0)


def test_costs_are_nondecreasing_in_quantity():
    model = _settings(
        default=AssetClassCosts(fee_flat=1.0, fee_bps=5.0, half_spread_bps=2.0),
        impact_bps=80.0,
    ).build()
    prev = None
    for q in (1.0, 10.0, 100.0, 1_000.0, 10_000.0):
        c = model.cost(_trade(quantity=q, bar_volume=5_000.0))
        if prev is not None:
            assert c.fill_price >= prev.fill_price
            assert c.fee >= prev.fee
        prev = c


# ---- settings validation ----------------------------------------------------


@pytest.mark.parametrize("field", ["fee_flat", "fee_bps", "half_spread_bps"])
def test_asset_class_costs_reject_negatives(field):
    with pytest.raises(ValidationError):
        AssetClassCosts(**{field: -1.0})


def test_settings_reject_negative_impact():
    with pytest.raises(ValidationError):
        CostModelSettings(impact_bps=-1.0)


def test_settings_reject_adverse_bps_that_would_make_sell_prices_non_positive():
    with pytest.raises(ValidationError):
        CostModelSettings(max_impact_bps=10_000.0)
    with pytest.raises(ValidationError):
        CostModelSettings(default=AssetClassCosts(half_spread_bps=6_000.0), max_impact_bps=5_000.0)


def test_realistic_preset_charges_every_asset_class():
    model = CostModelSettings.realistic().build()
    for asset_class in ("equity", "crypto", "commodity", "bond"):
        cost = model.cost(_trade(asset_class=asset_class, bar_volume=1_000_000.0))
        assert cost.fill_price > 100.0 or cost.fee > 0.0
    crypto = model.cost(_trade(asset_class="crypto"))
    equity = model.cost(_trade(asset_class="equity"))
    assert crypto.fee > equity.fee
