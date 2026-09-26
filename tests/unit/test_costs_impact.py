"""Volatility-aware impact and per-ticker spreads in ``AssetClassCostModel`` (BL-31)."""

from __future__ import annotations

import math

import numpy as np
import pytest
from pydantic import ValidationError

from stonks.backtest.costs import (
    AssetClassCosts,
    CostModelSettings,
    IStarSettings,
    Trade,
)


def _trade(**kw) -> Trade:
    base = {
        "ticker": "X.US",
        "side": "buy",
        "quantity": 1_000.0,
        "price": 100.0,
        "bar_volume": 50_000.0,
        "adv": 100_000.0,
        "sigma_daily": 0.02,
    }
    base.update(kw)
    return Trade(**base)


def _bps(settings: CostModelSettings, trade: Trade) -> float:
    cost = settings.build().cost(trade)
    return (cost.fill_price / trade.price - 1.0) * 10_000 * (1 if trade.side == "buy" else -1)


# ---- defaults are unchanged -----------------------------------------------------------


def test_default_impact_model_is_the_legacy_square_root_law():
    s = CostModelSettings(impact_bps=100.0)
    assert s.impact_model == "sqrt" and s.half_spread_model == "class"
    # adv / sigma / spread estimates are ignored by the legacy settings
    with_stats = _bps(s, _trade(half_spread_bps=50.0))
    without = _bps(s, _trade(adv=None, sigma_daily=None))
    assert with_stats == pytest.approx(without)
    assert with_stats == pytest.approx(100.0 * math.sqrt(1_000 / 50_000))


def test_legacy_settings_need_no_market_stats():
    assert CostModelSettings().build().market_stats_spec is None
    assert CostModelSettings.realistic().build().market_stats_spec is None


# ---- sqrt_vol -------------------------------------------------------------------------


def test_sqrt_vol_formula():
    s = CostModelSettings(impact_model="sqrt_vol", impact_gamma=1.0)
    expected = 1.0 * 0.02 * 10_000 * math.sqrt(1_000 / 100_000)
    assert _bps(s, _trade()) == pytest.approx(expected)


def test_sqrt_vol_ignores_the_fill_bar_volume():
    s = CostModelSettings(impact_model="sqrt_vol")
    assert _bps(s, _trade(bar_volume=10.0)) == pytest.approx(_bps(s, _trade(bar_volume=1e9)))


@pytest.mark.parametrize("model", ["sqrt_vol", "istar"])
def test_higher_volatility_costs_more_at_equal_participation(model):
    s = CostModelSettings(impact_model=model)
    assert _bps(s, _trade(sigma_daily=0.03)) > _bps(s, _trade(sigma_daily=0.01))


@pytest.mark.parametrize("model", ["sqrt_vol", "istar"])
def test_unknown_adv_or_sigma_falls_back_to_the_square_root_law(model):
    s = CostModelSettings(impact_model=model, impact_bps=100.0)
    legacy = CostModelSettings(impact_bps=100.0)
    for trade in (_trade(adv=None), _trade(sigma_daily=None), _trade(sigma_daily=float("nan"))):
        assert _bps(s, trade) == pytest.approx(_bps(legacy, trade))


@pytest.mark.parametrize("model", ["sqrt_vol", "istar"])
def test_zero_adv_pays_the_cap(model):
    s = CostModelSettings(impact_model=model, max_impact_bps=300.0)
    assert _bps(s, _trade(adv=0.0)) == pytest.approx(300.0)


@pytest.mark.parametrize("model", ["sqrt_vol", "istar"])
def test_impact_is_capped(model):
    s = CostModelSettings(impact_model=model, max_impact_bps=50.0)
    assert _bps(s, _trade(quantity=1e8, sigma_daily=0.5)) == pytest.approx(50.0)


# ---- istar ----------------------------------------------------------------------------


def test_istar_reduces_to_the_square_root_law():
    """a2 = 0.5, a3 = 0 and all-permanent impact (b1 = 0) is ``a1 sqrt(Q/ADV)``."""
    istar = CostModelSettings(
        impact_model="istar", istar=IStarSettings(a1=150.0, a2=0.5, a3=0.0, b1=0.0)
    )
    sqrt = CostModelSettings(impact_bps=150.0)
    trade = _trade()
    assert _bps(istar, trade) == pytest.approx(_bps(sqrt, _trade(bar_volume=trade.adv)))


def test_istar_formula_and_temporary_permanent_split():
    p = IStarSettings()
    assert (p.a1, p.a2, p.a3, p.a4, p.b1, p.pov) == (708.0, 0.55, 0.71, 0.5, 0.98, 0.1)
    model = CostModelSettings(impact_model="istar").build()
    trade = _trade()
    sigma_annual = 0.02 * math.sqrt(252)
    i_star = 708.0 * (1_000 / 100_000) ** 0.55 * sigma_annual**0.71
    temporary, permanent = model.impact_components(trade)
    assert temporary == pytest.approx(0.98 * i_star * 0.1**0.5)
    assert permanent == pytest.approx(0.02 * i_star)
    assert _bps(CostModelSettings(impact_model="istar"), trade) == pytest.approx(
        temporary + permanent
    )


def test_square_root_models_are_all_temporary():
    model = CostModelSettings(impact_model="sqrt_vol").build()
    temporary, permanent = model.impact_components(_trade())
    assert permanent == 0.0 and temporary > 0


def test_capped_components_scale_together():
    model = CostModelSettings(impact_model="istar", max_impact_bps=10.0).build()
    temporary, permanent = model.impact_components(_trade(quantity=1e7))
    assert temporary + permanent == pytest.approx(10.0)
    assert permanent / (temporary + permanent) == pytest.approx(0.02 / (0.98 * 0.1**0.5 + 0.02))


# ---- per-ticker spreads ---------------------------------------------------------------


def _spread_settings(**kw) -> CostModelSettings:
    return CostModelSettings(
        default=AssetClassCosts(half_spread_bps=2.0),
        half_spread_model="corwin_schultz",
        **kw,
    )


def test_per_ticker_half_spread_replaces_the_class_value():
    assert _bps(_spread_settings(), _trade(half_spread_bps=15.0)) == pytest.approx(15.0)


def test_per_ticker_half_spread_is_clipped_to_the_class_floor_and_cap():
    assert _bps(_spread_settings(), _trade(half_spread_bps=0.5)) == pytest.approx(2.0)
    assert _bps(_spread_settings(), _trade(half_spread_bps=900.0)) == pytest.approx(200.0)
    s = _spread_settings(max_half_spread_bps=50.0)
    assert _bps(s, _trade(half_spread_bps=900.0)) == pytest.approx(50.0)


def test_unknown_per_ticker_spread_uses_the_class_value():
    assert _bps(_spread_settings(), _trade(half_spread_bps=None)) == pytest.approx(2.0)
    assert _bps(_spread_settings(), _trade(half_spread_bps=float("nan"))) == pytest.approx(2.0)


def test_class_model_ignores_the_estimate():
    s = CostModelSettings(default=AssetClassCosts(half_spread_bps=2.0))
    assert _bps(s, _trade(half_spread_bps=15.0)) == pytest.approx(2.0)


def test_spread_applies_on_sells_too():
    assert _bps(_spread_settings(), _trade(side="sell", half_spread_bps=15.0)) == pytest.approx(
        15.0
    )


def test_market_stats_spec_follows_the_selected_models():
    spec = CostModelSettings(impact_model="sqrt_vol", adv_window=10, vol_window=15).build()
    assert spec.market_stats_spec.adv_window == 10
    assert spec.market_stats_spec.vol_window == 15
    assert spec.market_stats_spec.spread_estimator is None
    spread = _spread_settings(spread_window=30).build().market_stats_spec
    assert spread.spread_estimator == "corwin_schultz" and spread.spread_window == 30


def test_sell_prices_stay_positive_with_the_spread_cap():
    with pytest.raises(ValidationError):
        CostModelSettings(
            half_spread_model="abdi_ranaldo", max_half_spread_bps=9_600.0, max_impact_bps=500.0
        )


# ---- monotonicity contract ------------------------------------------------------------


@pytest.mark.parametrize("model", ["sqrt", "sqrt_vol", "istar"])
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_fill_price_and_fee_are_monotone_in_quantity(model, side):
    rng = np.random.default_rng(7)
    settings = CostModelSettings(
        default=AssetClassCosts(half_spread_bps=3.0, fee_bps=1.0, fee_flat=0.5),
        impact_model=model,
        impact_bps=80.0,
        half_spread_model="corwin_schultz",
    )
    cost_model = settings.build()
    for _ in range(50):
        adv = float(rng.uniform(1e3, 1e7))
        sigma = float(rng.uniform(0.001, 0.08))
        spread = float(rng.uniform(0.0, 300.0))
        quantities = np.sort(rng.uniform(1.0, adv * 2, 20))
        prev_px, prev_fee = None, None
        for q in quantities:
            c = cost_model.cost(
                _trade(
                    side=side,
                    quantity=float(q),
                    adv=adv,
                    sigma_daily=sigma,
                    half_spread_bps=spread,
                    bar_volume=adv / 2,
                )
            )
            adverse = c.fill_price if side == "buy" else -c.fill_price
            if prev_px is not None:
                assert adverse >= prev_px - 1e-12
                assert c.fee >= prev_fee - 1e-12 or side == "sell"
            prev_px, prev_fee = adverse, c.fee


# ---- RS-19: I-Star annualises with the trade's own calendar ---------------------------


def test_istar_uses_the_trade_periods_per_year():
    settings = CostModelSettings(impact_model="istar")
    daily = settings.build().impact_components(_trade())
    crypto = settings.build().impact_components(_trade(periods_per_year=365.0))
    hourly = settings.build().impact_components(_trade(periods_per_year=252.0 * 7))
    a3 = IStarSettings().a3
    assert sum(crypto) / sum(daily) == pytest.approx((365.0 / 252.0) ** (a3 / 2))
    assert sum(hourly) / sum(daily) == pytest.approx(7.0 ** (a3 / 2))
