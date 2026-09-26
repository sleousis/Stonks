"""Hand-worked examples for the per-order W3.1 rules: risk per position
(k x ATR and one-day 99% VaR), liquidity and sector caps (BL-27)."""

from __future__ import annotations

import math

import pytest

from stonks.core.types import Portfolio
from stonks.production.rules import registered_rules
from tests.fixtures.risk_rules import alternating, bars, buy, context, policy, sell


def _rule(name: str):
    [rule] = [r for r in registered_rules() if r.name == name]
    return rule


# ---- risk_per_position ----------------------------------------------------------


def test_atr_risk_clips_a_buy():
    # high - low = 2 every bar and flat closes: ATR = 2. 2% of 100k / (3 x 2).
    hist = {"A.US": bars([100.0] * 60, spread=1.0)}
    ctx = context(
        Portfolio(cash=100_000.0),
        {"A.US": 100.0},
        policy(risk_per_position={"max_risk": 0.02}),
        history=hist,
    )
    kept, adj = _rule("risk_per_position").apply([buy("A.US", 1_000)], ctx)
    assert kept[0].quantity == pytest.approx(2_000.0 / 6.0)
    assert adj[0].rule == "risk_per_position"
    assert "ATR" in adj[0].reason


def test_atr_risk_counts_the_held_quantity():
    hist = {"A.US": bars([100.0] * 60, spread=1.0)}
    ctx = context(
        Portfolio(cash=90_000.0, positions={"A.US": 100.0}),
        {"A.US": 100.0},
        policy(risk_per_position={"max_risk": 0.02, "atr_multiple": 2.0}),
        history=hist,
    )
    kept, _ = _rule("risk_per_position").apply([buy("A.US", 1_000)], ctx)
    assert kept[0].quantity == pytest.approx(2_000.0 / 4.0 - 100.0)


def test_var_cap_clips_a_buy():
    # log returns alternate +-1%: zero-mean EWMA sigma is exactly 1%.
    hist = {"A.US": bars(alternating(120))}
    price = float(hist["A.US"]["close"].iloc[-1])
    ctx = context(
        Portfolio(cash=1_000_000.0),
        {"A.US": price},
        policy(risk_per_position={"max_var": 0.001}),
        history=hist,
    )
    kept, adj = _rule("risk_per_position").apply([buy("A.US", 10_000)], ctx)
    assert kept[0].quantity == pytest.approx(1_000.0 / (price * 0.01 * 2.33), rel=1e-6)
    assert adj[0].rule == "position_var"


def test_risk_per_position_drops_a_buy_without_enough_history():
    ctx = context(
        Portfolio(cash=100_000.0),
        {"A.US": 100.0},
        policy(risk_per_position={"max_risk": 0.02}),
        history={"A.US": bars([100.0] * 5, spread=1.0)},
    )
    kept, adj = _rule("risk_per_position").apply([buy("A.US", 10)], ctx)
    assert kept == []
    assert adj[0].rule == "insufficient_history"


def test_risk_per_position_leaves_sells_and_small_buys():
    hist = {"A.US": bars([100.0] * 60, spread=1.0)}
    ctx = context(
        Portfolio(cash=100_000.0, positions={"A.US": 50.0}),
        {"A.US": 100.0},
        policy(risk_per_position={"max_risk": 0.02}),
        history=hist,
    )
    orders = [sell("A.US", 50), buy("A.US", 10)]
    kept, adj = _rule("risk_per_position").apply(orders, ctx)
    assert kept == orders
    assert adj == []


def test_risk_per_position_is_off_by_default():
    ctx = context(Portfolio(cash=100.0), {"A.US": 100.0}, policy())
    assert not _rule("risk_per_position").enabled(ctx.policy)
    orders = [buy("A.US", 1_000)]
    assert _rule("risk_per_position").apply(orders, ctx) == (orders, [])


# ---- liquidity -----------------------------------------------------------------------


def test_liquidity_caps_a_buy_at_a_share_of_median_dollar_volume():
    hist = {"A.US": bars([10.0] * 30, volume=100_000.0)}  # $1m a day
    ctx = context(
        Portfolio(cash=1e9),
        {"A.US": 10.0},
        policy(liquidity={"max_pct_adv": 0.01}),
        history=hist,
    )
    kept, adj = _rule("liquidity").apply([buy("A.US", 5_000)], ctx)
    assert kept[0].quantity == pytest.approx(1_000.0)
    assert adj[0].rule == "max_pct_adv"


def test_liquidity_drops_an_illiquid_name():
    hist = {"A.US": bars([10.0] * 30, volume=100_000.0)}
    ctx = context(
        Portfolio(cash=1e9),
        {"A.US": 10.0},
        policy(liquidity={"min_median_dollar_volume": 2e6}),
        history=hist,
    )
    kept, adj = _rule("liquidity").apply([buy("A.US", 5)], ctx)
    assert kept == []
    assert adj[0].rule == "min_median_dollar_volume"


def test_liquidity_amihud_ceiling():
    closes = alternating(30, start=10.0)
    hist = {"A.US": bars(closes, volume=100_000.0)}
    # |r| = 1% on ~$1m: Amihud ~ 1e-8.
    ctx = context(
        Portfolio(cash=1e9),
        {"A.US": closes[-1]},
        policy(liquidity={"max_amihud": 1e-9}),
        history=hist,
    )
    kept, adj = _rule("liquidity").apply([buy("A.US", 5)], ctx)
    assert kept == []
    assert adj[0].rule == "max_amihud"
    loose = context(
        Portfolio(cash=1e9),
        {"A.US": closes[-1]},
        policy(liquidity={"max_amihud": 1e-7}),
        history=hist,
    )
    assert _rule("liquidity").apply([buy("A.US", 5)], loose)[1] == []


def test_liquidity_drops_a_buy_without_volume_history():
    ctx = context(
        Portfolio(cash=1e9),
        {"A.US": 10.0},
        policy(liquidity={"max_pct_adv": 0.01}),
        history={"A.US": bars([10.0] * 30, volume=math.nan)},
    )
    kept, adj = _rule("liquidity").apply([buy("A.US", 5)], ctx)
    assert kept == []
    assert adj[0].rule == "insufficient_history"


# ---- sector_cap ------------------------------------------------------------------------


def test_sector_cap_clips_buys_in_a_full_sector():
    ctx = context(
        Portfolio(cash=70_000.0, positions={"B.US": 300.0}),
        {"A.US": 100.0, "B.US": 100.0, "C.US": 100.0, "D.US": 100.0},
        policy(sector_cap={"max_weight_per_sector": 0.4}),
        sectors={"A.US": "Technology", "B.US": "Technology", "C.US": "Health Care"},
    )
    orders = [buy("A.US", 500), buy("C.US", 50), buy("D.US", 50)]
    kept, adj = _rule("sector_cap").apply(orders, ctx)
    assert [(o.ticker, o.quantity) for o in kept] == [
        ("A.US", pytest.approx(100.0)),
        ("C.US", 50),
        ("D.US", 50),  # no sector known: not capped
    ]
    assert [a.rule for a in adj] == ["max_weight_per_sector"]
    assert "Technology" in adj[0].reason


def test_sector_cap_counts_earlier_buys_of_the_same_tick():
    ctx = context(
        Portfolio(cash=100_000.0),
        {"A.US": 100.0, "B.US": 100.0},
        policy(sector_cap={"max_weight_per_sector": 0.3}),
        sectors={"A.US": "Tech", "B.US": "Tech"},
    )
    kept, _ = _rule("sector_cap").apply([buy("A.US", 200), buy("B.US", 200)], ctx)
    assert [o.quantity for o in kept] == [200, pytest.approx(100.0)]


def test_sector_cap_drops_a_buy_when_a_sector_holding_is_unpriced():
    ctx = context(
        Portfolio(cash=100_000.0, positions={"B.US": 10.0}),
        {"A.US": 100.0},
        policy(sector_cap={"max_weight_per_sector": 0.3}),
        sectors={"A.US": "Tech", "B.US": "Tech"},
    )
    kept, adj = _rule("sector_cap").apply([buy("A.US", 1)], ctx)
    assert kept == []
    assert adj[0].rule == "unpriced_holding"
