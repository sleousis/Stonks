"""Risk rule properties (BL-11, BL-27, BL-28, BL-49).

For every registered rule and for ``apply_risk`` as a whole, on any book,
prices, history, policy and proposed orders: no buy grows and no new buy
appears, gross exposure after the kept orders is never above gross after
the proposed ones, and an order that closes a position (a sell within the
long) is never dropped or shrunk.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from hypothesis import given
from hypothesis import strategies as st

from stonks.core.types import Order, Portfolio
from stonks.production.risk import apply_risk
from stonks.production.rules import registered_rules
from stonks.production.rules.drawdown_scaling import DEFAULT_SCHEDULE
from stonks.production.rules.settings import RuleSettings
from tests.fixtures.risk_rules import AS_OF, Policy, bars, buy, context, sell

TICKERS = ["A.US", "B.US", "C.US", "BTC-USD.CC"]
CLASSES = {t: ("crypto" if t.endswith(".CC") else "equity") for t in TICKERS}
SECTORS = {"A.US": "Tech", "B.US": "Tech", "C.US": "Energy"}
RULES = registered_rules()


@st.composite
def rule_settings(draw) -> RuleSettings:
    pick = lambda *xs: draw(st.sampled_from(xs))  # noqa: E731
    return RuleSettings.model_validate(
        {
            "risk_per_position": {
                "max_risk": pick(None, 0.0025, 0.02),
                "max_var": pick(None, 0.02),
            },
            "portfolio_vol": {"vol_cap": pick(None, 0.05, 0.25), "shock_cap": pick(None, 0.1)},
            "drawdown_scaling": {"schedule": pick(None, DEFAULT_SCHEDULE)},
            "liquidity": {
                "max_pct_adv": pick(None, 0.01),
                "min_median_dollar_volume": pick(None, 1e6),
            },
            "sector_cap": {"max_weight_per_sector": pick(None, 0.1, 0.5)},
            "max_holding": {"max_holding_bars": pick(None, 5, 50)},
            "circuit_breaker": {"max_drawdown_halt": pick(None, 0.1)},
            "gross_exposure": {"max_gross": pick(None, 1.0, 1.5)},
            "net_exposure": {"max_net": pick(None, 0.5)},
            "short_caps": {"max_short_weight": pick(None, 0.1)},
            "squeeze_guard": {"max_adverse_pct": pick(None, 0.2)},
            "margin_call": {"enabled": pick(False, True)},
        }
    )


@st.composite
def cases(draw):
    history, prices = {}, {}
    for t in TICKERS:
        n = draw(st.sampled_from([3, 30, 90]))
        drift = draw(st.floats(min_value=-0.01, max_value=0.01))
        start = draw(st.floats(min_value=5.0, max_value=500.0))
        closes = [start * (1 + drift) ** i for i in range(n)]
        vol = draw(st.sampled_from([1e3, 1e5, 1e7]))
        history[t] = bars(closes, spread=closes[-1] * 0.01, volume=vol)
        prices[t] = closes[-1]
    positions = {
        t: draw(st.sampled_from([1.0, 10.0, 100.0]))
        for t in draw(st.lists(st.sampled_from(TICKERS), unique=True, max_size=3))
    }
    portfolio = Portfolio(cash=draw(st.sampled_from([0.0, 1_000.0, 50_000.0])), positions=positions)
    orders: list[Order] = []
    left = dict(positions)
    for i in range(draw(st.integers(min_value=0, max_value=6))):
        t = draw(st.sampled_from(TICKERS))
        qty = draw(st.sampled_from([0.5, 3.0, 50.0, 1_000.0]))
        if draw(st.booleans()) or left.get(t, 0.0) <= 0:
            orders.append(buy(t, qty, i, tick_id="T"))
        else:
            q = min(qty, left[t])
            left[t] -= q
            orders.append(sell(t, q, i, tick_id="T"))
    value = portfolio.total_value(prices)
    scale = draw(st.floats(min_value=0.6, max_value=1.4))
    curve = [
        (AS_OF - timedelta(days=30 - i), value * (scale if i < 15 else 1.0)) for i in range(30)
    ]
    pol = Policy(
        max_open_positions=draw(st.sampled_from([None, 1, 2])),
        max_weight_per_ticker=draw(st.sampled_from([1.0, 0.3])),
        cash_buffer_fraction=draw(st.sampled_from([0.0, 0.1])),
        min_order_notional=draw(st.sampled_from([0.0, 50.0])),
        rules=draw(rule_settings()),
    )
    ctx = context(
        portfolio,
        prices,
        pol,
        asset_classes=CLASSES,
        history=history,
        sectors=SECTORS,
        equity_curve=curve,
        entry_dates={t: history[t].index[0].date() for t in positions},
    )
    return orders, ctx


def _gross(portfolio: Portfolio, orders, prices) -> float:
    book = dict(portfolio.positions)
    for o in orders:
        book[o.ticker] = book.get(o.ticker, 0.0) + (o.quantity if o.side == "buy" else -o.quantity)
    return sum(abs(q) * prices.get(t, 0.0) for t, q in book.items())


def _check(proposed, kept, ctx) -> None:
    before = {o.client_id: o.quantity for o in proposed if o.side == "buy"}
    after = {o.client_id: o.quantity for o in kept if o.side == "buy"}
    assert set(after) <= set(before)
    assert all(after[c] <= before[c] + 1e-9 for c in after)
    kept_sells = {o.client_id: o.quantity for o in kept if o.side == "sell"}
    for o in proposed:
        if o.side == "sell":  # every proposed sell is within the long: a close
            assert kept_sells.get(o.client_id) == o.quantity
    tolerance = 1e-9 * max(1.0, _gross(ctx.portfolio, proposed, ctx.prices))
    assert (
        _gross(ctx.portfolio, kept, ctx.prices)
        <= _gross(ctx.portfolio, proposed, ctx.prices) + tolerance
    )


@given(st.sampled_from(RULES), cases())
def test_no_rule_raises_gross_or_blocks_a_close(rule, case):
    orders, ctx = case
    kept, _ = rule.apply(orders, ctx)
    _check(orders, kept, ctx)


@given(cases())
def test_apply_risk_never_raises_gross_or_blocks_a_close(case):
    orders, ctx = case
    result = apply_risk(
        orders, ctx.portfolio, ctx.prices, ctx.asset_classes, ctx.policy, context=ctx
    )
    _check(orders, result.orders, ctx)


@st.composite
def short_cases(draw):
    """A book that may short: long and short positions and at most one order
    per ticker (as ``orders_from_targets`` makes them), which may cover a
    short, close a long, or cross zero and open the other side."""
    _, ctx = draw(cases())
    positions = dict(ctx.portfolio.positions)
    for t in draw(st.lists(st.sampled_from(TICKERS), unique=True, max_size=2)):
        positions[t] = -draw(st.sampled_from([1.0, 10.0, 100.0]))
    proposed = []
    for i, t in enumerate(TICKERS):
        side = draw(st.sampled_from([None, "buy", "sell"]))
        if side is not None:
            qty = draw(st.sampled_from([0.5, 5.0, 50.0, 500.0]))
            proposed.append(Order(client_id=f"o:{t}:{i}", ticker=t, side=side, quantity=qty))
    portfolio = Portfolio(cash=ctx.portfolio.cash, positions=positions)
    return proposed, replace(ctx, portfolio=portfolio, allow_short=True)


@given(short_cases())
def test_a_short_book_never_raises_gross_or_blocks_a_close(case):
    from stonks.execution.orders import classify_all

    orders, ctx = case
    proposed = classify_all(orders, ctx.portfolio.positions)
    result = apply_risk(
        orders,
        ctx.portfolio,
        ctx.prices,
        ctx.asset_classes,
        ctx.policy,
        context=ctx,
        allow_short=True,
    )
    kept = {o.client_id: o for o in result.orders}
    # closes measured against the starting book: per ticker, the orders on
    # the reducing side keep at least what reduces the starting position
    for ticker, held in ctx.portfolio.positions.items():
        reducing = "sell" if held > 0 else "buy"
        wanted = sum(o.quantity for o in proposed if o.ticker == ticker and o.side == reducing)
        got = sum(o.quantity for o in result.orders if o.ticker == ticker and o.side == reducing)
        assert got >= min(wanted, abs(held)) - 1e-9 * max(1.0, abs(held))
    opened = {o.client_id: o.quantity for o in proposed if o.position_effect == "open"}
    for cid, o in kept.items():
        if cid in opened:
            assert o.quantity <= opened[cid] + 1e-9
    before = _gross(ctx.portfolio, proposed, ctx.prices)
    after = _gross(ctx.portfolio, result.orders, ctx.prices)
    assert after <= before + 1e-9 * max(1.0, before)
