"""The live safeguards keep the risk-rule properties (roadmap 19.6, P28).

The generic property test runs every rule without a live context, where
the live rules do nothing. Here each book is live: an allocation, the
account, quotes (live, delayed or none) and what was sent today are drawn
too. No buy grows, no new buy appears, gross never rises, and no close is
dropped or shrunk.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from hypothesis import given
from hypothesis import strategies as st

from stonks.execution.brokers.base import LiveAccountState, Quote
from stonks.production.live.context import LiveContext
from stonks.production.risk import apply_risk
from stonks.production.rules import registered_rules
from stonks.production.rules.settings import RuleSettings
from tests.property.test_risk_rule_properties import TICKERS, _check, cases

NOW = datetime(2026, 9, 28, 13, 0, tzinfo=UTC)
LIVE_RULES = [
    r
    for r in registered_rules()
    if r.name in {"capital_ramp", "live_notional_caps", "price_band", "max_orders_per_run"}
]


@st.composite
def live_cases(draw):
    orders, ctx = draw(cases())
    pick = lambda *xs: draw(st.sampled_from(xs))  # noqa: E731
    quotes = {}
    for t in TICKERS:
        kind = pick("none", "live", "delayed")
        base = ctx.prices[t] * pick(0.9, 1.0, 1.1)
        if kind != "none":
            quotes[t] = Quote(
                t,
                last=base,
                bid=base * 0.999,
                ask=base * 1.001,
                as_of=NOW,
                delayed=kind == "delayed",
            )
    equity = pick(1_000.0, 100_000.0)
    account = LiveAccountState(
        equity=equity,
        cash=equity,
        settled_cash=equity,
        available_funds=equity,
        buying_power=equity,
        currency="USD",
        account_type="cash",
    )
    live = LiveContext(
        portfolio_id="pf_live",
        allocation=pick(None, 0.0, 500.0, 50_000.0),
        account=pick(None, account),
        quotes=quotes,
        sent_today=pick(0.0, 900.0),
    )
    rules = ctx.policy.rules.model_dump()
    rules.update(
        capital_ramp={"enabled": True},
        live_notional_caps={
            "max_order_notional": pick(None, 1_000.0),
            "max_day_notional": pick(None, 2_000.0),
        },
        price_band={"band_pct": 0.02, "max_gap_pct": pick(None, 0.05)},
        max_orders_per_run={
            "max_opening_orders": pick(None, 0, 2),
            "max_closing_orders": pick(None, 0, 1),
        },
    )
    policy = ctx.policy.model_copy(update={"rules": RuleSettings.model_validate(rules)})
    orders = [
        replace(o, decision_price=ctx.prices[o.ticker], decision_context={"score": float(i)})
        for i, o in enumerate(orders)
    ]
    return orders, replace(ctx, live=live, policy=policy)


@given(st.sampled_from(LIVE_RULES), live_cases())
def test_a_live_rule_never_raises_gross_or_blocks_a_close(rule, case):
    orders, ctx = case
    kept, _ = rule.apply(orders, ctx)
    _check(orders, kept, ctx)


@given(live_cases())
def test_apply_risk_on_a_live_book_never_raises_gross_or_blocks_a_close(case):
    orders, ctx = case
    result = apply_risk(
        orders, ctx.portfolio, ctx.prices, ctx.asset_classes, ctx.policy, context=ctx
    )
    _check(orders, result.orders, ctx)
