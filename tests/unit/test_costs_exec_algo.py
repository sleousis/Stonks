"""The backtest's execution algo assumption (roadmap 23.16): the cost model
scales spread and impact by the algo's factors and adds its timing cost."""

from __future__ import annotations

import pytest

from stonks.backtest.costs import (
    AssetClassCosts,
    CostModelSettings,
    ExecAlgoAssumption,
    Trade,
)

BASE = CostModelSettings(default=AssetClassCosts(half_spread_bps=4.0), impact_bps=100.0)
TRADE = Trade(ticker="A", side="buy", quantity=100, price=100.0, bar_volume=10_000)


def _bps(settings: CostModelSettings) -> float:
    return (settings.build().cost(TRADE).fill_price / 100.0 - 1.0) * 10_000


def test_no_algo_keeps_the_plain_cost():
    assert _bps(BASE) == pytest.approx(4.0 + 10.0)


def test_vwap_halves_the_impact_and_adds_timing():
    vwap = BASE.model_copy(update={"exec_algo": ExecAlgoAssumption(name="vwap")})
    assert _bps(vwap) == pytest.approx(4.0 + 5.0 + 2.0)


def test_patient_adaptive_pays_half_the_spread():
    adaptive = BASE.model_copy(
        update={"exec_algo": ExecAlgoAssumption(name="adaptive", params={"priority": "patient"})}
    )
    assert _bps(adaptive) == pytest.approx(2.0 + 10.0 + 1.0)


def test_sell_side_moves_the_other_way():
    vwap = BASE.model_copy(update={"exec_algo": ExecAlgoAssumption(name="vwap")}).build()
    sell = vwap.cost(Trade(ticker="A", side="sell", quantity=100, price=100.0, bar_volume=10_000))
    assert sell.fill_price == pytest.approx(100.0 * (1 - 11.0 / 10_000))


def test_unknown_algo_is_refused_in_settings():
    with pytest.raises(ValueError):
        ExecAlgoAssumption(name="iceberg")
    with pytest.raises(ValueError):
        CostModelSettings.model_validate({"exec_algo": {"name": "vwap", "params": {"slices": 0}}})
