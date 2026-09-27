"""The live safeguards (roadmap 19.6): registered risk rules that act only
on live books (``RiskContext.live`` set), only shrink or drop opening
orders, and never drop a closing order (P28)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from stonks.core.types import Portfolio
from stonks.execution.brokers.base import LiveAccountState, Quote
from stonks.production.live.context import LiveContext
from stonks.production.live.quotes import reference_price
from stonks.production.risk import apply_risk
from stonks.production.rules import registered_rules
from tests.fixtures.risk_rules import buy, context, policy, sell

NOW = datetime(2026, 9, 28, 13, 0, tzinfo=UTC)
PRICES = {"A.US": 100.0, "B.US": 50.0, "C.US": 20.0}


def live(**kw) -> LiveContext:
    kw.setdefault("portfolio_id", "pf_live")
    kw.setdefault("allocation", 1_000_000.0)
    return LiveContext(**kw)


def rule(name):
    return next(r for r in registered_rules() if r.name == name)


def account(equity=1_000_000.0) -> LiveAccountState:
    return LiveAccountState(
        equity=equity,
        cash=equity,
        settled_cash=equity,
        available_funds=equity,
        buying_power=equity,
        currency="USD",
        account_type="cash",
    )


LIVE_RULES = ("capital_ramp", "live_notional_caps", "price_band", "max_orders_per_run")
ALL_ON = {
    "capital_ramp": {"enabled": True},
    "live_notional_caps": {"max_order_notional": 1_000.0, "max_day_notional": 5_000.0},
    "price_band": {"band_pct": 0.02, "max_gap_pct": 0.05},
    "max_orders_per_run": {"max_opening_orders": 2, "max_closing_orders": 3},
}


def test_the_live_rules_are_registered_and_off_by_default():
    names = {r.name for r in registered_rules()}
    assert set(LIVE_RULES) <= names
    pol = policy()
    assert not any(rule(n).enabled(pol) for n in LIVE_RULES)


@pytest.mark.parametrize("name", LIVE_RULES)
def test_a_paper_or_backtest_book_is_left_alone(name):
    pol = policy(**ALL_ON)
    orders = [buy("A.US", 500.0, 1), buy("B.US", 5.0, 2), buy("C.US", 1.0, 3)]
    ctx = context(Portfolio(cash=1e6), PRICES, pol)
    kept, adjustments = rule(name).apply(orders, ctx)
    assert kept == orders and adjustments == []


# ---- capital_ramp: the owner's allocation cap ---------------------------------------


def test_capital_ramp_caps_gross_at_the_allocation():
    pol = policy(capital_ramp={"enabled": True})
    book = Portfolio(cash=100_000.0, positions={"A.US": 50.0})  # 5,000 held
    ctx = context(book, PRICES, pol, live=live(allocation=10_000.0))
    kept, adj = rule("capital_ramp").apply([buy("B.US", 200.0)], ctx)  # 10,000 more
    assert kept[0].quantity == pytest.approx(100.0)  # 5,000 of room
    assert adj and adj[0].rule == "capital_ramp"


def test_capital_ramp_uses_the_smaller_of_allocation_and_net_liquidation():
    pol = policy(capital_ramp={"enabled": True})
    ctx = context(
        Portfolio(cash=0.0),
        PRICES,
        pol,
        live=live(allocation=10_000.0, account=account(equity=2_000.0)),
    )
    kept, _ = rule("capital_ramp").apply([buy("A.US", 100.0)], ctx)
    assert kept[0].quantity == pytest.approx(20.0)


def test_no_allocation_means_nothing_opens_but_closes_pass():
    pol = policy(capital_ramp={"enabled": True})
    book = Portfolio(cash=0.0, positions={"A.US": 10.0})
    ctx = context(book, PRICES, pol, live=live(allocation=None))
    orders = [sell("A.US", 10.0), buy("B.US", 1.0)]
    kept, adj = rule("capital_ramp").apply(orders, ctx)
    assert kept == [orders[0]]
    assert "no live allocation" in adj[0].reason


# ---- live_notional_caps ----------------------------------------------------------------


def test_per_order_and_per_day_caps_clip_opening_orders():
    pol = policy(live_notional_caps={"max_order_notional": 1_000.0, "max_day_notional": 1_500.0})
    ctx = context(Portfolio(cash=1e6), PRICES, pol, live=live(sent_today=200.0))
    orders = [buy("A.US", 50.0, 1), buy("B.US", 40.0, 2), buy("C.US", 10.0, 3)]
    kept, adj = rule("live_notional_caps").apply(orders, ctx)
    got = {o.ticker: o.quantity for o in kept}
    assert got["A.US"] == pytest.approx(10.0)  # 1,000 per order
    assert got["B.US"] == pytest.approx(6.0)  # 300 left of the day's 1,500
    assert "C.US" not in got  # the day cap is used up
    assert {a.rule for a in adj} == {"live_notional_caps"}


def test_user_and_global_day_caps_apply_on_top():
    pol = policy(
        live_notional_caps={"max_user_day_notional": 1_000.0, "max_global_day_notional": 600.0}
    )
    ctx = context(
        Portfolio(cash=1e6),
        PRICES,
        pol,
        live=live(sent_today_user=100.0, sent_today_global=400.0),
    )
    kept, _ = rule("live_notional_caps").apply([buy("A.US", 10.0)], ctx)
    assert kept[0].quantity == pytest.approx(2.0)  # 200 left globally


def test_caps_never_touch_a_closing_order():
    pol = policy(live_notional_caps={"max_order_notional": 10.0, "max_day_notional": 10.0})
    book = Portfolio(cash=0.0, positions={"A.US": 100.0})
    ctx = context(book, PRICES, pol, live=live(sent_today=1e9))
    orders = [sell("A.US", 100.0)]
    kept, adj = rule("live_notional_caps").apply(orders, ctx)
    assert kept == orders and adj == []


# ---- price_band ---------------------------------------------------------------------


def test_the_band_sets_a_collared_limit_from_the_live_quote():
    pol = policy(price_band={"band_pct": 0.02, "nbbo_band_pct": 0.01})
    quotes = {
        "A.US": Quote("A.US", last=101.0, bid=100.9, ask=101.1, as_of=NOW, delayed=False),
    }
    book = Portfolio(cash=1e6, positions={"B.US": 10.0})
    ctx = context(book, PRICES, pol, live=live(quotes=quotes))
    kept, adj = rule("price_band").apply([buy("A.US", 1.0), sell("B.US", 10.0)], ctx)
    a, b = kept
    assert a.order_type == "limit"
    # min(last x 1.02, ask x 1.01)
    assert a.limit_price == pytest.approx(min(101.0 * 1.02, 101.1 * 1.01))
    # B has no live quote: the lake close with the tighter delayed band
    assert b.order_type == "limit"
    assert b.limit_price == pytest.approx(50.0 * (1 - 0.01))
    assert b.quantity == 10.0
    assert all(x.adjusted_quantity == x.original_quantity for x in adj)


def test_the_band_clamps_an_existing_limit_but_never_loosens_it():
    pol = policy(price_band={"band_pct": 0.02})
    ctx = context(Portfolio(cash=1e6), PRICES, pol, live=live())
    wide = replace(buy("A.US", 1.0, 1), order_type="limit", limit_price=150.0)
    tight = replace(buy("A.US", 1.0, 2), order_type="limit", limit_price=95.0)
    kept, _ = rule("price_band").apply([wide, tight], ctx)
    assert kept[0].limit_price == pytest.approx(101.0)  # 100 x (1 + delayed 1%)
    assert kept[1].limit_price == 95.0


def test_a_gap_since_the_decision_drops_an_opening_order_only():
    pol = policy(price_band={"band_pct": 0.02, "max_gap_pct": 0.05})
    quotes = {
        t: Quote(t, last=p * 1.10, bid=None, ask=None, as_of=NOW, delayed=False)
        for t, p in PRICES.items()
    }
    book = Portfolio(cash=1e6, positions={"B.US": 5.0})
    ctx = context(book, PRICES, pol, live=live(quotes=quotes))
    opening = replace(buy("A.US", 1.0), decision_price=100.0)
    closing = replace(sell("B.US", 5.0), decision_price=50.0)
    kept, adj = rule("price_band").apply([opening, closing], ctx)
    assert [o.ticker for o in kept] == ["B.US"]
    assert any("moved" in a.reason and a.ticker == "A.US" for a in adj)


def test_no_reference_drops_an_open_and_keeps_a_close_unbanded():
    pol = policy(price_band={"band_pct": 0.02})
    book = Portfolio(cash=1e6, positions={"Z.US": 3.0})
    ctx = context(book, {}, pol, live=live())
    kept, adj = rule("price_band").apply([buy("Y.US", 1.0), sell("Z.US", 3.0)], ctx)
    assert [o.ticker for o in kept] == ["Z.US"]
    assert kept[0].order_type == "market"
    assert {a.ticker for a in adj} == {"Y.US", "Z.US"}


def test_reference_price_prefers_a_live_quote():
    pol = policy()
    live_q = Quote("A.US", last=None, bid=99.0, ask=101.0, as_of=NOW, delayed=False)
    delayed_q = Quote("B.US", last=55.0, bid=None, ask=None, as_of=NOW, delayed=True)
    ctx = context(
        Portfolio(cash=0.0), PRICES, pol, live=live(quotes={"A.US": live_q, "B.US": delayed_q})
    )
    a = reference_price(ctx, "A.US")
    assert a is not None and a.price == pytest.approx(100.0) and a.source == "live"
    b = reference_price(ctx, "B.US")
    assert b is not None and b.price == 55.0 and b.source == "delayed"
    c = reference_price(ctx, "C.US")
    assert c is not None and c.price == 20.0 and c.source == "close"
    assert reference_price(ctx, "Q.US") is None


# ---- max_orders_per_run ---------------------------------------------------------------


def test_opening_orders_beyond_the_limit_are_dropped_in_score_order():
    pol = policy(max_orders_per_run={"max_opening_orders": 2})
    ctx = context(Portfolio(cash=1e6), PRICES, pol, live=live())
    orders = [
        replace(buy("A.US", 1.0, 1), decision_context={"score": 0.1}),
        replace(buy("B.US", 1.0, 2), decision_context={"score": 0.9}),
        replace(buy("C.US", 1.0, 3), decision_context={"score": 0.5}),
    ]
    kept, adj = rule("max_orders_per_run").apply(orders, ctx)
    assert [o.ticker for o in kept] == ["B.US", "C.US"]
    assert [a.ticker for a in adj] == ["A.US"]


def test_too_many_closes_flag_a_runaway_and_drop_every_open():
    pol = policy(max_orders_per_run={"max_opening_orders": 5, "max_closing_orders": 1})
    book = Portfolio(cash=1e6, positions={"A.US": 1.0, "B.US": 1.0})
    ctx = context(book, PRICES, pol, live=live())
    orders = [sell("A.US", 1.0), sell("B.US", 1.0), buy("C.US", 1.0)]
    kept, adj = rule("max_orders_per_run").apply(orders, ctx)
    assert [o.ticker for o in kept] == ["A.US", "B.US"]  # closes are never dropped
    assert any(a.rule == "runaway" for a in adj)
    assert any(a.ticker == "C.US" and a.adjusted_quantity == 0.0 for a in adj)


# ---- together -----------------------------------------------------------------------


def test_apply_risk_runs_every_live_rule_and_keeps_closes():
    pol = policy(**ALL_ON)
    book = Portfolio(cash=1e6, positions={"A.US": 30.0})
    ctx = context(book, PRICES, pol, live=live(allocation=50_000.0))
    orders = [sell("A.US", 30.0), buy("B.US", 100.0), buy("C.US", 10.0)]
    result = apply_risk(orders, book, PRICES, ctx.asset_classes, pol, context=ctx)
    closes = [o for o in result.orders if o.side == "sell"]
    assert closes and closes[0].quantity == 30.0
    for o in result.orders:
        assert o.order_type == "limit"
        if o.side == "buy":
            assert o.quantity * PRICES[o.ticker] <= 1_000.0 + 1e-6
