"""Roadmap 8.2: production simulated fills use the ``[backtest.costs]`` model.

Precedence (one resolver, ``SimulatedCosts.from_settings``):

1. ``[backtest.costs]`` present (even all zeros): that cost model, the one
   backtests use; ``[production]`` ``slippage_bps`` / ``fee_per_trade`` are
   ignored so costs are never charged twice.
2. otherwise: the legacy flat ``[production]`` slippage and fee.
"""

from __future__ import annotations

from datetime import date

import pytest

from stonks.backtest.costs import AssetClassCosts, CostModelSettings, Trade
from stonks.config import Settings, load_settings
from stonks.core.types import Order, Portfolio
from stonks.execution.brokers import make_broker
from stonks.execution.brokers.simulated import SimulatedCosts

LEGACY = {"slippage_bps": 5.0, "fee_per_trade": 1.0}


def _buy(broker, price=100.0, qty=10.0, volume=None, asset_class=None):
    if asset_class:
        broker.set_asset_classes({"X": asset_class})
    broker.set_prices({"X": price}, as_of=date(2026, 1, 2), volumes=volume and {"X": volume})
    return broker.place_order(Order(client_id="c", ticker="X", side="buy", quantity=qty))


def test_unset_backtest_costs_fall_back_to_production_slippage_and_fee():
    costs = SimulatedCosts.from_settings(Settings(production=LEGACY))
    assert costs == SimulatedCosts(model=None, slippage_bps=5.0, fee_per_trade=1.0)
    fill = _buy(costs.build_broker(Portfolio(cash=10_000.0)))
    assert fill.price == pytest.approx(100.05)
    assert fill.fee == pytest.approx(1.0)


def test_configured_backtest_costs_win_and_legacy_is_ignored():
    settings = Settings(production=LEGACY, backtest={"costs": CostModelSettings.realistic()})
    costs = SimulatedCosts.from_settings(settings)
    assert costs == SimulatedCosts(model=CostModelSettings.realistic())


def test_an_explicit_zero_cost_table_still_wins():
    settings = Settings(production=LEGACY, backtest={"costs": {}})
    assert SimulatedCosts.from_settings(settings) == SimulatedCosts(model=CostModelSettings())


def test_assigned_backtest_costs_count_as_configured():
    settings = Settings(production=LEGACY)
    settings.backtest.costs = CostModelSettings.realistic()
    assert SimulatedCosts.from_settings(settings).model == CostModelSettings.realistic()


def test_toml_backtest_costs_table_is_detected(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        "[production]\nslippage_bps = 5.0\n\n[backtest.costs]\nimpact_bps = 50.0\n",
        encoding="utf-8",
    )
    costs = SimulatedCosts.from_settings(load_settings(cfg))
    assert costs.model == CostModelSettings(impact_bps=50.0)
    assert (costs.slippage_bps, costs.fee_per_trade) == (0.0, 0.0)


def test_model_and_legacy_together_are_refused():
    with pytest.raises(ValueError, match="not both"):
        SimulatedCosts(model=CostModelSettings(), slippage_bps=1.0)


def test_model_broker_charges_asset_class_costs_and_volume_impact():
    model = CostModelSettings(
        asset_classes={"crypto": AssetClassCosts(half_spread_bps=10.0, fee_bps=20.0)},
        impact_bps=100.0,
    )
    broker = SimulatedCosts(model=model).build_broker(Portfolio(cash=10_000.0))
    fill = _buy(broker, volume=1_000.0, asset_class="crypto")
    expected = model.build().cost(
        Trade(
            ticker="X",
            side="buy",
            quantity=10.0,
            price=100.0,
            asset_class="crypto",
            bar_volume=1_000.0,
        )
    )
    assert fill.price == pytest.approx(expected.fill_price)
    assert fill.fee == pytest.approx(expected.fee)
    assert fill.price > 100.0 * (1 + 10.0 / 10_000)  # impact on top of the spread


def test_make_broker_simulated_uses_the_same_resolver():
    settings = Settings(production=LEGACY, backtest={"costs": CostModelSettings.realistic()})
    fill = _buy(make_broker(settings, Portfolio(cash=10_000.0)))
    expected = (
        CostModelSettings.realistic()
        .build()
        .cost(Trade(ticker="X", side="buy", quantity=10.0, price=100.0))
    )
    assert fill.price == pytest.approx(expected.fill_price)
    assert fill.fee == pytest.approx(expected.fee)
