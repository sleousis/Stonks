"""RegimeFilter (BL-42): a k-of-n composite regime gate around any strategy."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from stonks.core.params import ParameterSpec
from stonks.core.protocols import Strategy
from stonks.core.types import Order, Portfolio
from stonks.features.regime_conditions import RegimeCondition
from stonks.lab.catalog import is_wrapper, strategy_catalog
from stonks.strategies.base import BaseStrategy
from stonks.strategies.regime import RegimeFilter
from tests.unit.nt888_helpers import make_lake, write_bars

INNER = "tests.unit.test_regime_filter:TwoWayInner"
AS_OF = datetime(2024, 3, 1)


class FixedCondition(RegimeCondition):
    """A test condition whose answer is set in its spec."""

    kind = "test_fixed"
    value: bool | None = True

    def triggered(self, as_of, ctx):
        return self.value


class TwoWayInner(BaseStrategy):
    """Buys every pick with 100 shares and sells what it holds but no
    longer picks."""

    id = "two_way"
    applicable_asset_classes = ("equity", "crypto")

    @classmethod
    def parameter_spec(cls):
        return [ParameterSpec(name="qty", kind="float", default=100.0, bounds=(1.0, 1e6))]

    def estimate_return(self, ticker, as_of, lake):
        return 0.1

    def decide(self, my_picks, portfolio, prices, as_of):
        picked = {t for _, t in my_picks}
        orders = [
            Order(client_id=f"s:{t}", ticker=t, side="sell", quantity=q)
            for t, q in portfolio.positions.items()
            if q > 0 and t not in picked
        ]
        orders += [
            Order(client_id=f"b:{t}", ticker=t, side="buy", quantity=self.params["qty"])
            for t in sorted(picked)
            if portfolio.positions.get(t, 0.0) <= 0
        ]
        return orders


class _Lake:
    """Weakly referenceable stand-in; the fixed conditions never read it."""


def _fixed(*values):
    return [{"kind": "test_fixed", "value": v} for v in values]


def _filter(conditions, **overrides) -> RegimeFilter:
    return RegimeFilter(
        {"inner_class_path": INNER, "inner_params": {}, "conditions": conditions, **overrides}
    )


def _run(f: RegimeFilter, positions=None):
    lake = _Lake()
    est = f.estimate_return("NEW", AS_OF, lake)
    portfolio = Portfolio(cash=1e6, positions=positions or {"OLD": 5.0})
    picks = [(0.1, "NEW")] if est is not None else []
    orders = f.decide(picks, portfolio, {"NEW": 10.0, "OLD": 10.0}, AS_OF)
    return est, orders, lake


# ---- surface -------------------------------------------------------------------------


def test_protocol_id_and_asset_classes_mirror_the_inner():
    f = _filter(_fixed(False))
    assert isinstance(f, Strategy)
    assert f.id == "two_way_regime"
    assert f.applicable_asset_classes == ("equity", "crypto")
    assert f.hypothesis


def test_defaults_and_tunable_knobs():
    f = RegimeFilter({"inner_class_path": INNER, "inner_params": {}})
    assert f.params["k"] == 1
    assert f.params["mode"] == "block_new_buys"
    assert f.params["conditions"] == [{"kind": "price_trend", "ticker": "SPY.US"}]
    tunable = {s.name for s in RegimeFilter.parameter_spec() if s.tunable}
    assert {"k", "trend_sma", "trend_hysteresis", "vol_window", "vol_pct", "htf_ema"} <= tunable


def test_tunable_knobs_reach_the_conditions():
    f = RegimeFilter(
        {
            "inner_class_path": INNER,
            "inner_params": {},
            "conditions": [{"kind": "price_trend", "ticker": "X"}],
            "trend_sma": 120,
        }
    )
    assert f.conditions[0].sma == 120


def test_bad_conditions_and_k_are_rejected():
    with pytest.raises(ValueError, match="unknown regime condition"):
        _filter([{"kind": "nope"}])
    with pytest.raises(ValueError, match="at least one"):
        _filter([])
    # RS-29: a k above the number of conditions is clamped, not rejected
    assert _filter(_fixed(True, True), k=3).params["k"] == 2


# ---- k of n --------------------------------------------------------------------------


def test_risk_off_needs_at_least_k_triggered():
    lake = _Lake()
    assert _filter(_fixed(True, False, False), k=1).is_risk_off(AS_OF, lake)
    assert not _filter(_fixed(True, False, False), k=2).is_risk_off(AS_OF, lake)
    assert _filter(_fixed(True, True, False), k=2).is_risk_off(AS_OF, lake)
    assert _filter(_fixed(True, False, False), k=1).triggered_count(AS_OF, lake) == (1, 3)


def test_unknown_conditions_count_per_when_unknown():
    lake = _Lake()
    assert not _filter(_fixed(None), k=1).is_risk_off(AS_OF, lake)
    assert _filter(_fixed(None), k=1, when_unknown="trigger").is_risk_off(AS_OF, lake)


def test_risk_on_is_transparent():
    est, orders, _ = _run(_filter(_fixed(False)))
    assert est == 0.1
    assert {(o.side, o.ticker, o.quantity) for o in orders} == {
        ("sell", "OLD", 5.0),
        ("buy", "NEW", 100.0),
    }


# ---- modes ---------------------------------------------------------------------------


def test_block_new_buys_drops_buys_and_lets_sells_through():
    est, orders, _ = _run(_filter(_fixed(True), mode="block_new_buys"))
    assert est == 0.1  # holdings stay picked, so the inner does not dump them
    assert [(o.side, o.ticker) for o in orders] == [("sell", "OLD")]


def test_exit_all_flattens_every_long():
    f = _filter(_fixed(True), mode="exit_all")
    est, orders, _ = _run(f, positions={"OLD": 5.0, "NEW": 2.0})
    assert est is None
    assert sorted((o.side, o.ticker, o.quantity) for o in orders) == [
        ("sell", "NEW", 2.0),
        ("sell", "OLD", 5.0),
    ]


def test_scale_multiplies_buys_by_the_share_not_triggered():
    f = _filter(_fixed(True, False, False, False), mode="scale", k=1)
    est, orders, _ = _run(f)
    buys = [o for o in orders if o.side == "buy"]
    sells = [o for o in orders if o.side == "sell"]
    assert est == 0.1
    assert buys[0].quantity == pytest.approx(75.0)
    assert sells[0].quantity == 5.0


def test_scale_drops_buys_when_every_condition_triggers():
    _, orders, _ = _run(_filter(_fixed(True, True), mode="scale"))
    assert [o.side for o in orders] == ["sell"]


def test_decide_without_a_lake_is_transparent():
    f = _filter(_fixed(True), mode="exit_all")
    orders = f.decide([(0.1, "NEW")], Portfolio(cash=1e6, positions={}), {"NEW": 10.0}, AS_OF)
    assert [o.side for o in orders] == ["buy"]


def test_features_report_the_regime():
    f = _filter(_fixed(True, False), k=1)
    values = f.extract_features("NEW", AS_OF, _Lake()).values
    assert values["regime_triggered"] == 1.0
    assert values["regime_risk_off"] == 1.0


# ---- persistence and catalog ---------------------------------------------------------


def test_save_load_round_trip(tmp_path):
    f = _filter(_fixed(True, False), k=2, mode="scale")
    f.save(tmp_path)
    back = RegimeFilter.load(tmp_path)
    assert back.params == f.params
    assert back.id == f.id
    assert (tmp_path / "inner" / "params.json").exists()


def test_registered_in_the_catalog_as_a_wrapper():
    catalog = strategy_catalog()
    assert catalog["regime_filter"] is RegimeFilter
    assert is_wrapper(RegimeFilter)


def test_real_price_trend_condition_on_a_lake(tmp_path):
    dates = pd.bdate_range("2020-01-01", periods=300)
    closes = np.concatenate([100 * np.exp(0.002 * np.arange(200)), np.zeros(100)])
    closes[200:] = closes[199] * np.exp(-0.01 * np.arange(1, 101))
    lake = make_lake(tmp_path / "lake.duckdb")
    write_bars(lake, "IDX.US", dates, closes)
    f = _filter([{"kind": "price_trend", "ticker": "IDX.US"}], trend_sma=50)
    assert not f.is_risk_off(dates[190].to_pydatetime(), lake)
    assert f.is_risk_off(dates[299].to_pydatetime(), lake)
    lake.close()
