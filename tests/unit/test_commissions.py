"""IBKR fee schedules and US regulatory sell fees in the cost model (roadmap 23.2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from stonks.backtest.commissions import (
    COMMISSIONS,
    CommissionSettings,
    IbkrFixedFees,
    IbkrTieredFees,
    UsRegulatoryFees,
    commission_fee,
    regulatory_fee,
)
from stonks.backtest.costs import AssetClassCosts, CostModelSettings, Trade
from stonks.production.tca import expected_cost_bps


def _trade(**kw) -> Trade:
    base = {"ticker": "AAPL.US", "side": "buy", "quantity": 100.0, "price": 50.0}
    base.update(kw)
    return Trade(**base)


# ---- schedules ----------------------------------------------------------------------


def test_registry_names_the_schedules():
    assert {"none", "ibkr_fixed", "ibkr_tiered"} <= set(COMMISSIONS)


def test_none_charges_nothing():
    assert commission_fee("none", _trade(), 50.0, CommissionSettings()) == 0.0


def test_ibkr_fixed_is_per_share():
    # 100 shares x 0.005 = 0.50, below the 1.00 minimum
    fee = commission_fee("ibkr_fixed", _trade(quantity=100), 50.0, CommissionSettings())
    assert fee == pytest.approx(1.00)
    # 1000 shares x 0.005 = 5.00
    fee = commission_fee("ibkr_fixed", _trade(quantity=1000), 50.0, CommissionSettings())
    assert fee == pytest.approx(5.00)


def test_ibkr_fixed_cap_is_one_percent_of_trade_value():
    # 10,000 penny shares at 0.10: 50.00 per share fee, capped at 1% of 1,000
    fee = commission_fee("ibkr_fixed", _trade(quantity=10_000), 0.10, CommissionSettings())
    assert fee == pytest.approx(10.0)


def test_ibkr_cap_wins_over_the_minimum():
    # a 20 dollar trade: the 1% cap (0.20) is below the 1.00 minimum and wins
    fee = commission_fee("ibkr_fixed", _trade(quantity=1), 20.0, CommissionSettings())
    assert fee == pytest.approx(0.20)


def test_ibkr_tiered_adds_exchange_and_clearing_per_share():
    s = CommissionSettings(
        ibkr_tiered=IbkrTieredFees(exchange_per_share=0.003, clearing_per_share=0.0002)
    )
    fee = commission_fee("ibkr_tiered", _trade(quantity=1000), 50.0, s)
    assert fee == pytest.approx(1000 * 0.0035 + 1000 * 0.003 + 1000 * 0.0002)


def test_ibkr_tiered_minimum_and_volume_tiers():
    s = CommissionSettings(ibkr_tiered=IbkrTieredFees(exchange_per_share=0, clearing_per_share=0))
    assert commission_fee("ibkr_tiered", _trade(quantity=10), 50.0, s) == pytest.approx(0.35)
    busy = CommissionSettings(
        ibkr_tiered=IbkrTieredFees(
            exchange_per_share=0, clearing_per_share=0, monthly_shares=5_000_000
        )
    )
    assert commission_fee("ibkr_tiered", _trade(quantity=1000), 50.0, busy) == pytest.approx(1.5)


def test_tiers_must_ascend():
    with pytest.raises(ValidationError):
        IbkrTieredFees(tiers=((1000, 0.002), (500, 0.001)))


def test_fees_never_fall_with_quantity():
    s = CommissionSettings()
    for name in COMMISSIONS:
        fees = [commission_fee(name, _trade(quantity=q), 3.0, s) for q in range(1, 3000, 7)]
        assert fees == sorted(fees), name


# ---- US regulatory fees ---------------------------------------------------------------


def test_buys_pay_only_the_cat_fee():
    fees = UsRegulatoryFees(cat_per_share=0.000035)
    assert regulatory_fee(_trade(side="buy", quantity=100), 50.0, fees) == pytest.approx(0.0035)


def test_sells_pay_sec_taf_and_cat():
    fees = UsRegulatoryFees(sec_rate_per_million=27.80, taf_per_share=0.000166, cat_per_share=0)
    fee = regulatory_fee(_trade(side="sell", quantity=1000), 50.0, fees)
    assert fee == pytest.approx(50_000 * 27.80 / 1e6 + 1000 * 0.000166)


def test_taf_is_capped_per_trade():
    fees = UsRegulatoryFees(
        sec_rate_per_million=0, taf_per_share=0.000166, taf_max=8.30, cat_per_share=0
    )
    assert regulatory_fee(_trade(side="sell", quantity=1_000_000), 1.0, fees) == pytest.approx(8.30)


# ---- the cost model ------------------------------------------------------------------


def test_asset_class_commission_is_added_to_the_fee():
    settings = CostModelSettings(
        asset_classes={"equity": AssetClassCosts(commission="ibkr_fixed", us_sell_fees=True)},
        commissions=CommissionSettings(
            us_regulatory=UsRegulatoryFees(
                sec_rate_per_million=0, taf_per_share=0.0001, cat_per_share=0
            )
        ),
    )
    model = settings.build()
    buy = model.cost(_trade(side="buy", quantity=1000))
    sell = model.cost(_trade(side="sell", quantity=1000))
    assert buy.fee == pytest.approx(5.0)
    assert sell.fee == pytest.approx(5.0 + 0.1)


def test_default_settings_are_unchanged():
    model = CostModelSettings.realistic().build()
    cost = model.cost(_trade())
    assert cost.fee == pytest.approx(0.5 / 10_000 * cost.fill_price * 100)


def test_unknown_commission_is_refused():
    with pytest.raises(ValidationError):
        AssetClassCosts(commission="robinhood")


def test_ibkr_presets_charge_commission_on_equities_only():
    for preset in (CostModelSettings.ibkr("tiered"), CostModelSettings.ibkr("fixed")):
        equity = preset.for_asset_class("equity")
        assert equity.commission.startswith("ibkr_")
        assert equity.us_sell_fees
        assert equity.fee_bps == 0.0
        assert preset.for_asset_class("crypto").commission == "none"


def test_tca_expected_cost_includes_the_commission():
    plain = CostModelSettings().build()
    ibkr = CostModelSettings(
        asset_classes={"equity": AssetClassCosts(commission="ibkr_fixed")}
    ).build()
    trade = _trade(quantity=1000, price=50.0)
    assert expected_cost_bps(plain, trade) == pytest.approx(0.0)
    assert expected_cost_bps(ibkr, trade) == pytest.approx(5.0 / 50_000 * 10_000)


def test_fixed_settings_validate():
    with pytest.raises(ValidationError):
        IbkrFixedFees(per_share=-1)


# ---- zero-cost checks see commissions ------------------------------------------------


def test_lab_sees_a_commission_only_model_as_costly():
    from stonks.lab.runner import costs_are_zero

    assert costs_are_zero(CostModelSettings())
    only_fees = CostModelSettings(
        default=AssetClassCosts(commission="ibkr_fixed"),
    )
    assert not costs_are_zero(only_fees)
    assert not costs_are_zero(CostModelSettings(default=AssetClassCosts(us_sell_fees=True)))


def test_golive_counts_a_commission_as_a_cost():
    from stonks.production.golive import _nonzero_cost_inputs

    zero = CostModelSettings().model_dump(mode="json")
    assert _nonzero_cost_inputs(zero) == []
    ibkr = CostModelSettings(default=AssetClassCosts(commission="ibkr_tiered")).model_dump(
        mode="json"
    )
    assert _nonzero_cost_inputs(ibkr) == ["commission"]
