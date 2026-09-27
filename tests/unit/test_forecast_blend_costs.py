"""ForecastBlend reads trade costs through a seam (roadmap 22.10): the lab,
the backtest and the tick hand it their cost model, and without one it
uses realistic defaults. It never reads config itself."""

from __future__ import annotations

import pytest

from stonks.backtest.costs import AssetClassCosts, CostModelSettings
from stonks.strategies.costs import CostAware, bind_costs, trade_costs_or_default
from stonks.strategies.examples.forecast_blend import ForecastBlend
from stonks.strategies.trailing_stop import TrailingStopWrapper
from tests.unit.trend_helpers import LAST, build_lake, trend

FREE = CostModelSettings(
    default=AssetClassCosts(),
    asset_classes={"crypto": AssetClassCosts(half_spread_bps=0.1)},
)
DEAR = CostModelSettings(
    default=AssetClassCosts(half_spread_bps=40.0),
    asset_classes={"equity": AssetClassCosts(half_spread_bps=40.0, fee_bps=10.0)},
)


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    db = build_lake(
        tmp_path_factory.mktemp("blend_costs") / "lake.duckdb",
        {"UP.US": trend(0.002, seed=1), "COIN.CC": trend(0.002, seed=1)},
        asset_classes={"UP.US": "equity", "COIN.CC": "crypto"},
    )
    yield db
    db.close()


def _dropped(strategy: ForecastBlend, ticker: str, lake) -> set[str]:
    strategy.forecast(ticker, LAST, lake)
    return {r.name for r in strategy.forecast_weight_fits()[ticker].rules if r.dropped}


def test_without_a_cost_model_the_realistic_defaults_apply(lake):
    plain = _dropped(ForecastBlend({}), "COIN.CC", lake)
    given = _dropped(ForecastBlend({}, costs=CostModelSettings.realistic()), "COIN.CC", lake)
    assert plain == given and "ewmac2" in plain


def test_all_zero_costs_fall_back_to_the_defaults():
    realistic = CostModelSettings.realistic()
    assert trade_costs_or_default(None) == realistic
    assert trade_costs_or_default(CostModelSettings()) == realistic
    assert trade_costs_or_default(DEAR) is DEAR


def test_a_cheaper_cost_model_keeps_the_fast_rules(lake):
    realistic = _dropped(ForecastBlend({}), "COIN.CC", lake)
    cheap = _dropped(ForecastBlend({}, costs=FREE), "COIN.CC", lake)
    assert "ewmac2" in realistic and "ewmac2" not in cheap
    assert cheap < realistic


def test_a_dearer_cost_model_drops_more_rules(lake):
    realistic = _dropped(ForecastBlend({}), "UP.US", lake)
    dear = _dropped(ForecastBlend({}, costs=DEAR), "UP.US", lake)
    assert dear > realistic


def test_binding_refits_with_the_new_costs(lake):
    s = ForecastBlend({})
    before = _dropped(s, "COIN.CC", lake)
    assert isinstance(s, CostAware)
    assert bind_costs(s, FREE) is s
    assert _dropped(s, "COIN.CC", lake) < before


def test_bind_costs_reaches_a_wrapped_strategy(lake):
    wrapper = TrailingStopWrapper(
        {
            "inner_class_path": "stonks.strategies.examples.forecast_blend:ForecastBlend",
            "inner_params": {},
        }
    )
    bind_costs(wrapper, FREE)
    inner = wrapper.inner
    assert isinstance(inner, ForecastBlend)
    assert inner.costs == FREE


def test_bind_costs_ignores_none_and_other_strategies():
    s = ForecastBlend({}, costs=FREE)
    bind_costs(s, None)
    assert s.costs == FREE
    other = object()
    assert bind_costs(other, FREE) is other


def test_the_cost_model_survives_save_and_load(tmp_path):
    s = ForecastBlend({}, costs=DEAR)
    s.save(tmp_path / "s")
    loaded = ForecastBlend.load(tmp_path / "s")
    assert isinstance(loaded, ForecastBlend) and loaded.costs == DEAR
    ForecastBlend({}).save(tmp_path / "plain")
    assert ForecastBlend.load(tmp_path / "plain").costs is None
