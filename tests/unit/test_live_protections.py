"""Per-strategy protections for live books (roadmap 19.6): a cooldown after
a stop-out, a pause after N stop-outs, and a lock on a ticker that keeps
losing. Off by default, opens only, never a close."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from stonks.core.types import Portfolio
from stonks.production.live.context import LiveContext
from stonks.production.live.trades import ClosedTrade, FillRow, closed_from_fills
from stonks.production.rules import registered_rules
from tests.fixtures.risk_rules import AS_OF, buy, context, policy, sell

PRICES = {"A.US": 10.0, "B.US": 20.0}


def rule(name):
    return next(r for r in registered_rules() if r.name == name)


def trade(ticker, days_ago, pnl, strategy="s1", stop=False):
    return ClosedTrade(
        strategy_id=strategy,
        ticker=ticker,
        exit_day=date.fromordinal(AS_OF.toordinal() - days_ago),
        pnl=pnl,
        stop=stop,
    )


def ctx_with(trades, book=None, **rules):
    live = LiveContext(portfolio_id="pf", closed_trades=tuple(trades))
    return context(book or Portfolio(cash=1e6), PRICES, policy(**rules), live=live)


def strat(order, sid="s1"):
    return replace(order, strategy_id=sid)


@pytest.mark.parametrize("name", ["stop_cooldown", "stop_guard", "losing_lock"])
def test_off_by_default_and_idle_on_paper_books(name):
    assert not rule(name).enabled(policy())
    on = {
        "stop_cooldown": {"cooldown_days": 5},
        "stop_guard": {"max_stops": 1},
        "losing_lock": {"max_consecutive_losses": 1},
    }[name]
    pol = policy(**{name: on})
    paper = context(Portfolio(cash=1e6), PRICES, pol)
    orders = [strat(buy("A.US", 1.0))]
    assert rule(name).apply(orders, paper) == (orders, [])


def test_cooldown_after_a_stop_out_on_the_same_ticker_and_strategy():
    trades = [trade("A.US", 2, -5.0)]
    ctx = ctx_with(trades, stop_cooldown={"cooldown_days": 5})
    orders = [
        strat(buy("A.US", 1.0, 1)),
        strat(buy("B.US", 1.0, 2)),
        strat(buy("A.US", 1, 3), "s2"),
    ]
    kept, adj = rule("stop_cooldown").apply(orders, ctx)
    assert [(o.ticker, o.strategy_id) for o in kept] == [("B.US", "s1"), ("A.US", "s2")]
    assert "cooling down" in adj[0].reason
    # an old stop-out, or a win, does not cool down
    assert (
        len(
            rule("stop_cooldown").apply(
                orders, ctx_with([trade("A.US", 9, -5.0)], stop_cooldown={"cooldown_days": 5})
            )[0]
        )
        == 3
    )
    assert (
        len(
            rule("stop_cooldown").apply(
                orders, ctx_with([trade("A.US", 1, 5.0)], stop_cooldown={"cooldown_days": 5})
            )[0]
        )
        == 3
    )


def test_count_losses_off_needs_a_real_stop():
    settings = {"cooldown_days": 5, "count_losses": False}
    orders = [strat(buy("A.US", 1.0))]
    assert (
        rule("stop_cooldown").apply(
            orders, ctx_with([trade("A.US", 1, -5.0)], stop_cooldown=settings)
        )[0]
        == orders
    )
    kept, _ = rule("stop_cooldown").apply(
        orders, ctx_with([trade("A.US", 1, 5.0, stop=True)], stop_cooldown=settings)
    )
    assert kept == []


def test_stop_guard_pauses_a_strategy_after_n_stop_outs():
    trades = [trade("A.US", 1, -1.0), trade("B.US", 3, -1.0), trade("A.US", 30, -1.0)]
    ctx = ctx_with(trades, stop_guard={"max_stops": 2, "window_days": 7})
    orders = [strat(buy("A.US", 1.0, 1)), strat(buy("B.US", 1.0, 2), "s2")]
    kept, adj = rule("stop_guard").apply(orders, ctx)
    assert [o.strategy_id for o in kept] == ["s2"]
    assert "s1 paused: 2 stop-outs" in adj[0].reason
    # a whole-book order counts every strategy
    book_order = strat(buy("B.US", 1.0, 3), "portfolio")
    assert rule("stop_guard").apply([book_order], ctx)[0] == []


def test_losing_lock_after_consecutive_losses():
    lose3 = [trade("A.US", d, -1.0) for d in (20, 10, 2)]
    ctx = ctx_with(lose3, losing_lock={"max_consecutive_losses": 3, "lock_days": 30})
    kept, adj = rule("losing_lock").apply([strat(buy("A.US", 1.0))], ctx)
    assert kept == [] and "locked" in adj[0].reason
    broken = [trade("A.US", 20, -1.0), trade("A.US", 10, 1.0), trade("A.US", 2, -1.0)]
    ctx = ctx_with(broken, losing_lock={"max_consecutive_losses": 3})
    assert rule("losing_lock").apply([strat(buy("A.US", 1.0))], ctx)[0]
    stale = [trade("A.US", d, -1.0) for d in (90, 80, 70)]
    ctx = ctx_with(stale, losing_lock={"max_consecutive_losses": 3, "lock_days": 30})
    assert rule("losing_lock").apply([strat(buy("A.US", 1.0))], ctx)[0]


@pytest.mark.parametrize(
    ("name", "settings"),
    [
        ("stop_cooldown", {"cooldown_days": 30}),
        ("stop_guard", {"max_stops": 1}),
        ("losing_lock", {"max_consecutive_losses": 1}),
    ],
)
def test_a_close_is_never_blocked(name, settings):
    book = Portfolio(cash=0.0, positions={"A.US": 5.0})
    ctx = ctx_with([trade("A.US", 1, -9.0)], book=book, **{name: settings})
    close = strat(sell("A.US", 5.0))
    assert rule(name).apply([close], ctx) == ([close], [])


def test_closed_trades_at_average_cost_long_and_short():
    d = date(2026, 9, 1)
    fills = [
        FillRow("s1", "A.US", "buy", 10.0, 10.0, d),
        FillRow("s1", "A.US", "buy", 10.0, 12.0, d),
        FillRow("s1", "A.US", "sell", 5.0, 10.0, date(2026, 9, 2)),  # avg 11: -5
        FillRow("s1", "A.US", "sell", 20.0, 13.0, date(2026, 9, 3), stop=True),  # 15 at +2, 5 short
        FillRow("s1", "A.US", "buy", 5.0, 12.0, date(2026, 9, 4)),  # cover the short: +5
        FillRow("s2", "A.US", "buy", 1.0, 1.0, d),
    ]
    out = closed_from_fills(fills)
    assert [(t.exit_day.day, t.pnl, t.stop) for t in out] == [
        (2, pytest.approx(-5.0), False),
        (3, pytest.approx(30.0), True),
        (4, pytest.approx(5.0), False),
    ]
    assert out[0].loss and not out[1].loss


# ---- 19.10: real broker stops replace "any losing exit" ------------------------------


def test_with_protective_stops_on_only_real_stop_outs_count():
    orders = [strat(buy("A.US", 1.0))]
    stops_on = {"enabled": True}
    loss = [trade("A.US", 1, -5.0)]
    ctx = ctx_with(loss, stop_cooldown={"cooldown_days": 5}, protective_stops=stops_on)
    assert rule("stop_cooldown").apply(orders, ctx)[0] == orders
    stopped = [trade("A.US", 1, -5.0, stop=True)]
    ctx = ctx_with(stopped, stop_cooldown={"cooldown_days": 5}, protective_stops=stops_on)
    assert rule("stop_cooldown").apply(orders, ctx)[0] == []
    guard = ctx_with(loss * 3, stop_guard={"max_stops": 2}, protective_stops=stops_on)
    assert rule("stop_guard").apply(orders, guard)[0] == orders


def test_count_losses_true_keeps_counting_losses_with_stops_on():
    orders = [strat(buy("A.US", 1.0))]
    ctx = ctx_with(
        [trade("A.US", 1, -5.0)],
        stop_cooldown={"cooldown_days": 5, "count_losses": True},
        protective_stops={"enabled": True},
    )
    assert rule("stop_cooldown").apply(orders, ctx)[0] == []


def test_count_losses_merges_towards_counting():
    from stonks.production.rules.settings import RuleSettings, tighter_rule_settings

    base = RuleSettings.model_validate({"stop_guard": {"max_stops": 2, "count_losses": False}})
    auto = tighter_rule_settings(base, {"stop_guard": {"count_losses": None}})
    assert auto.stop_guard.count_losses is None
    always = tighter_rule_settings(auto, {"stop_guard": {"count_losses": True}})
    assert always.stop_guard.count_losses is True
    assert tighter_rule_settings(always, {"stop_guard": {"count_losses": False}}) == always


# ---- 23.15: the per-ticker loss breaker ----------------------------------------------


def notional_trade(ticker, days_ago, pnl, notional, strategy="s1"):
    return replace(trade(ticker, days_ago, pnl, strategy), notional=notional)


def test_closed_trades_carry_the_entry_notional_they_closed():
    d = date(2026, 9, 1)
    fills = [
        FillRow("s1", "A.US", "buy", 10.0, 10.0, d),
        FillRow("s1", "A.US", "sell", 4.0, 9.0, date(2026, 9, 2)),
        FillRow("s1", "A.US", "sell", 6.0, 12.0, date(2026, 9, 3)),
    ]
    out = closed_from_fills(fills)
    assert [t.notional for t in out] == [pytest.approx(40.0), pytest.approx(60.0)]


def test_loss_breaker_locks_a_ticker_after_its_realised_loss_passes_the_limit():
    # -6 on 100 of notional: 6% lost, over a 5% limit
    trades = [notional_trade("A.US", 10, -8.0, 50.0), notional_trade("A.US", 3, 2.0, 50.0)]
    ctx = ctx_with(trades, losing_lock={"max_loss_pct": 0.05, "lock_days": 30})
    assert rule("losing_lock").enabled(ctx.policy)
    kept, adj = rule("losing_lock").apply([strat(buy("A.US", 1.0)), strat(buy("B.US", 1.0))], ctx)
    assert [o.ticker for o in kept] == ["B.US"]
    assert "6.0%" in adj[0].reason and "5.0%" in adj[0].reason


def test_loss_breaker_stays_open_under_the_limit_and_after_the_window():
    small = [notional_trade("A.US", 3, -4.0, 100.0)]
    ctx = ctx_with(small, losing_lock={"max_loss_pct": 0.05})
    assert rule("losing_lock").apply([strat(buy("A.US", 1.0))], ctx)[0]
    old = [notional_trade("A.US", 60, -40.0, 100.0)]
    ctx = ctx_with(old, losing_lock={"max_loss_pct": 0.05, "loss_window_days": 30})
    assert rule("losing_lock").apply([strat(buy("A.US", 1.0))], ctx)[0]


def test_loss_breaker_counts_only_the_strategy_of_the_order():
    trades = [notional_trade("A.US", 3, -10.0, 100.0, strategy="s2")]
    ctx = ctx_with(trades, losing_lock={"max_loss_pct": 0.05})
    assert rule("losing_lock").apply([strat(buy("A.US", 1.0))], ctx)[0]
    assert rule("losing_lock").apply([strat(buy("A.US", 1.0), "s2")], ctx)[0] == []


def test_loss_breaker_never_blocks_a_close():
    book = Portfolio(cash=0.0, positions={"A.US": 5.0})
    ctx = ctx_with(
        [notional_trade("A.US", 1, -50.0, 100.0)], book=book, losing_lock={"max_loss_pct": 0.01}
    )
    close = strat(sell("A.US", 5.0))
    assert rule("losing_lock").apply([close], ctx) == ([close], [])


def test_loss_breaker_merges_tighter():
    from stonks.production.rules.settings import RuleSettings, tighter_rule_settings

    base = RuleSettings.model_validate({"losing_lock": {"max_loss_pct": 0.1}})
    tight = tighter_rule_settings(base, {"losing_lock": {"max_loss_pct": 0.05}})
    assert tight.losing_lock.max_loss_pct == 0.05
    assert tighter_rule_settings(tight, {"losing_lock": {"max_loss_pct": 0.2}}) == tight
    # a longer window can hide a recent loss behind old gains: kept from the base
    longer = tighter_rule_settings(tight, {"losing_lock": {"loss_window_days": 365}})
    assert longer.losing_lock.loss_window_days == tight.losing_lock.loss_window_days
