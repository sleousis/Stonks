"""Hand-worked examples for the book-level W3.1 rules: drawdown scaling
(with hysteresis), portfolio volatility targeting and max holding time
(BL-27)."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd
import pytest

from stonks.core.types import Portfolio
from stonks.production.rules import registered_rules
from stonks.production.rules.drawdown_scaling import DEFAULT_SCHEDULE, drawdown_scale
from tests.fixtures.risk_rules import AS_OF, alternating, bars, buy, context, policy, sell


def _rule(name: str):
    [rule] = [r for r in registered_rules() if r.name == name]
    return rule


def _curve(*values: float) -> list[tuple[date, float]]:
    start = AS_OF - timedelta(days=len(values) + 1)
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


# ---- drawdown_scaling -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "scale"),
    [
        ([100.0], 1.0),
        ([100.0, 94.0], 1.0),  # 6%: first level still sizes at 1.0
        ([100.0, 89.0], 0.5),
        ([100.0, 84.0], 0.25),
        ([100.0, 89.0, 93.0], 0.5),  # 7% on the way back: held (hysteresis)
        ([100.0, 89.0, 96.0], 1.0),  # below 5%: released
        ([100.0, 84.0, 89.5], 0.25),  # 10.5%: 0.25 held until below 10%
        ([100.0, 84.0, 91.0], 0.5),
        ([100.0, 89.0, 120.0, 110.0], 1.0),  # new peak resets
    ],
)
def test_drawdown_scale_steps_down_fast_and_up_slowly(values, scale):
    assert drawdown_scale(values, DEFAULT_SCHEDULE)[0] == scale


def test_drawdown_scale_does_not_flap_around_a_threshold():
    path = [100.0] + [89.5 if i % 2 else 90.5 for i in range(20)]
    scales = [drawdown_scale(path[: i + 1], DEFAULT_SCHEDULE)[0] for i in range(1, len(path))]
    assert scales == [1.0] + [0.5] * (len(scales) - 1)


def test_drawdown_scaling_halves_buys_and_leaves_sells():
    pf = Portfolio(cash=88_000.0, positions={"B.US": 10.0})
    ctx = context(
        pf,
        {"A.US": 10.0, "B.US": 0.0001},
        policy(drawdown_scaling={"schedule": DEFAULT_SCHEDULE}),
        equity_curve=_curve(100_000.0, 95_000.0),
    )
    kept, adj = _rule("drawdown_scaling").apply([sell("B.US", 10), buy("A.US", 100)], ctx)
    assert [(o.side, o.quantity) for o in kept] == [("sell", 10), ("buy", 50.0)]
    assert [a.rule for a in adj] == ["drawdown_scaling"]
    assert "drawdown" in adj[0].reason


def test_drawdown_scaling_ignores_curve_points_after_as_of():
    pf = Portfolio(cash=100_000.0)
    later = [(AS_OF + timedelta(days=5), 200_000.0)]
    ctx = context(
        pf,
        {"A.US": 10.0},
        policy(drawdown_scaling={"schedule": DEFAULT_SCHEDULE}),
        equity_curve=_curve(100_000.0) + later,
    )
    kept, adj = _rule("drawdown_scaling").apply([buy("A.US", 100)], ctx)
    assert kept[0].quantity == 100 and adj == []


def test_drawdown_schedule_must_be_ordered():
    with pytest.raises(ValueError):
        policy(drawdown_scaling={"schedule": [(0.10, 0.5), (0.05, 1.0)]})
    with pytest.raises(ValueError):
        policy(drawdown_scaling={"schedule": [(0.05, 0.5), (0.10, 0.8)]})


# ---- portfolio_vol ------------------------------------------------------------------------

_ANNUAL = 0.01 * math.sqrt(252)  # sigma of +-1% alternating returns


def _vol_ctx(pf: Portfolio, **settings):
    hist = {"A.US": bars(alternating(250))}
    price = float(hist["A.US"]["close"].iloc[-1])
    return (
        context(pf, {"A.US": price}, policy(portfolio_vol=settings), history=hist),
        price,
    )


def test_portfolio_vol_scales_buys_to_the_cap():
    ctx, price = _vol_ctx(Portfolio(cash=100_000.0), vol_cap=0.10)
    kept, adj = _rule("portfolio_vol").apply([buy("A.US", 100_000.0 / price)], ctx)
    assert kept[0].quantity * price / 100_000.0 == pytest.approx(0.10 / _ANNUAL, rel=0.02)
    assert adj[0].rule == "portfolio_vol"


def test_portfolio_vol_counts_existing_holdings():
    ctx0, price = _vol_ctx(Portfolio(cash=0.0), vol_cap=0.10)
    held = 50_000.0 / price
    ctx, _ = _vol_ctx(Portfolio(cash=50_000.0, positions={"A.US": held}), vol_cap=0.10)
    kept, _ = _rule("portfolio_vol").apply([buy("A.US", held)], ctx)
    # (0.5 + 0.5 s) sigma = cap
    assert kept[0].quantity / held == pytest.approx(2 * 0.10 / _ANNUAL - 1, rel=0.05)


def test_portfolio_vol_correlation_shock_bound():
    ctx, price = _vol_ctx(Portfolio(cash=100_000.0), shock_cap=0.08)
    kept, adj = _rule("portfolio_vol").apply([buy("A.US", 100_000.0 / price)], ctx)
    assert kept[0].quantity * price / 100_000.0 == pytest.approx(0.08 / _ANNUAL, rel=0.02)
    assert adj[0].rule == "correlation_shock"


def test_portfolio_vol_under_the_cap_changes_nothing():
    ctx, price = _vol_ctx(Portfolio(cash=100_000.0), vol_cap=0.5, shock_cap=0.5)
    orders = [buy("A.US", 100_000.0 / price)]
    assert _rule("portfolio_vol").apply(orders, ctx) == (orders, [])


def test_portfolio_vol_drops_buys_without_history():
    ctx, price = _vol_ctx(Portfolio(cash=100_000.0), vol_cap=0.5)
    ctx = type(ctx)(**{**ctx.__dict__, "prices": {"A.US": price, "N.US": 10.0}})
    kept, adj = _rule("portfolio_vol").apply([buy("A.US", 1), buy("N.US", 1)], ctx)
    assert [o.ticker for o in kept] == ["A.US"]
    assert [a.rule for a in adj] == ["insufficient_history"]


def test_portfolio_vol_ignores_bars_after_as_of():
    ctx, price = _vol_ctx(Portfolio(cash=100_000.0), vol_cap=0.10)
    orders = [buy("A.US", 100_000.0 / price)]
    base, _ = _rule("portfolio_vol").apply(orders, ctx)
    wild = bars(
        [price * (3.0 if i % 2 else 0.3) for i in range(10)], end=AS_OF + timedelta(days=14)
    )
    polluted = pd.concat([ctx.history["A.US"], wild])
    ctx2 = type(ctx)(**{**ctx.__dict__, "history": {"A.US": polluted}})
    assert _rule("portfolio_vol").apply(orders, ctx2)[0] == base


# ---- max_holding ----------------------------------------------------------------------------


def _hold_ctx(entry_bars_ago: int, **kw):
    hist = {"A.US": bars([10.0] * 60)}
    entry = hist["A.US"].index[-1 - entry_bars_ago].date()
    return context(
        Portfolio(cash=0.0, positions={"A.US": 10.0}),
        {"A.US": 10.0},
        policy(max_holding={"max_holding_bars": 20}),
        history=hist,
        entry_dates={"A.US": entry},
        **kw,
    )


def test_max_holding_forces_a_sell_of_an_old_position():
    kept, adj = _rule("max_holding").apply([], _hold_ctx(25))
    [order] = kept
    assert (order.ticker, order.side, order.quantity) == ("A.US", "sell", 10.0)
    assert order.client_id == f"{AS_OF.isoformat()}:risk.max_holding:A.US:sell"
    assert order.strategy_id is None
    assert adj[0].rule == "max_holding"
    assert (adj[0].original_quantity, adj[0].adjusted_quantity) == (0.0, 10.0)


def test_max_holding_tops_up_a_partial_sell_and_drops_buys():
    orders = [sell("A.US", 3, tick_id="T1"), buy("A.US", 5, tick_id="T1")]
    kept, adj = _rule("max_holding").apply(orders, _hold_ctx(20))
    assert [(o.side, o.quantity) for o in kept] == [("sell", 3), ("sell", 7.0)]
    assert kept[1].tick_id == "T1"
    assert sorted(a.side for a in adj) == ["buy", "sell"]


def test_max_holding_leaves_a_young_position():
    orders = [buy("A.US", 5)]
    assert _rule("max_holding").apply(orders, _hold_ctx(19)) == (orders, [])
