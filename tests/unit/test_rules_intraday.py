"""The intraday risk rules (roadmap 21.3.2): the per-minute loss limit,
intraday drawdown scaling, the order rate cap and the stale data gate.
Every one is off by default, acts only on an intraday book (a context with
``intraday``), and never drops or shrinks a closing order."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from stonks.config import RiskPolicy
from stonks.core.types import Order, Portfolio
from stonks.production.risk import apply_risk
from stonks.production.rules import registered_rules
from stonks.production.rules._intraday import IntradayContext
from stonks.production.rules.intraday_drawdown import IntradayDrawdown
from stonks.production.rules.intraday_loss import (
    INTRADAY_FLATTEN,
    IntradayLossLimit,
    IntradayLossLimitSettings,
    loss_breach,
)
from stonks.production.rules.intraday_orders import IntradayOrderRate
from stonks.production.rules.intraday_stale import IntradayStaleData
from stonks.production.rules.settings import RuleSettings, tighter_rule_settings
from tests.fixtures.risk_rules import buy, context, policy, sell

NOW = datetime(2026, 9, 28, 15, 0, tzinfo=UTC)
INTRADAY_RULES = {
    "intraday_loss_limit",
    "intraday_drawdown",
    "intraday_order_rate",
    "intraday_stale_data",
}


def _marks(*values: float, step: int = 1) -> tuple[tuple[datetime, float], ...]:
    """Equity marks one ``step`` minute apart, the last one a minute before NOW."""
    n = len(values)
    return tuple(
        (NOW - timedelta(minutes=step * (n - i)), float(v)) for i, v in enumerate(values)
    )


def _ctx(pol, *, value: float = 10_000.0, positions=None, **intraday):
    """A book worth ``value`` (cash plus ``positions`` at 100 each)."""
    positions = positions or {}
    cash = value - 100.0 * sum(positions.values())
    prices = dict.fromkeys(["A.US", "B.US", *positions], 100.0)
    intraday.setdefault("now", NOW)
    return context(
        Portfolio(cash=cash, positions=positions),
        prices,
        pol,
        as_of=NOW.date(),
        intraday=IntradayContext(**intraday),
    )


# ---- defaults ------------------------------------------------------------------------


def test_every_intraday_rule_is_registered_and_off_by_default():
    rules = {r.name: r for r in registered_rules()}
    assert INTRADAY_RULES <= set(rules)
    for name in INTRADAY_RULES:
        assert not rules[name].enabled(RiskPolicy()), name


def test_the_rules_do_nothing_without_an_intraday_context():
    pol = policy(
        intraday_loss_limit={"max_loss": 0.001},
        intraday_drawdown={"schedule": [(0.001, 0.0)]},
        intraday_order_rate={"max_orders_per_minute": 1},
        intraday_stale_data={"max_bar_age_seconds": 1},
    )
    ctx = replace(_ctx(pol), intraday=None)
    orders = [buy("A.US", 5, 1), buy("B.US", 5, 2)]
    for rule in (IntradayLossLimit(), IntradayDrawdown(), IntradayOrderRate(), IntradayStaleData()):
        assert rule.apply(orders, ctx) == (orders, []), rule.name


def test_apply_risk_skips_them_without_a_context():
    pol = policy(intraday_stale_data={"max_bar_age_seconds": 30})
    result = apply_risk([buy("A.US", 1)], Portfolio(cash=1e4), {"A.US": 10.0}, {}, pol)
    assert "intraday_stale_data" in result.skipped_rules
    assert result.orders[0].quantity == 1


# ---- per-minute loss limit -----------------------------------------------------------


def test_a_loss_within_the_window_drops_opening_orders_and_keeps_closes():
    pol = policy(intraday_loss_limit={"max_loss": 0.02, "window_minutes": 5})
    ctx = _ctx(
        pol, value=9_700.0, positions={"A.US": 10}, equity_marks=_marks(10_000, 9_900, 9_800)
    )
    orders = [buy("B.US", 5, 1), sell("A.US", 4, 2)]
    kept, adj = IntradayLossLimit().apply(orders, ctx)
    assert kept == [orders[1]]
    assert [a.rule for a in adj] == ["intraday_loss"]
    assert "3.00%" in adj[0].reason


def test_a_loss_outside_the_window_does_not_count():
    pol = policy(intraday_loss_limit={"max_loss": 0.02, "window_minutes": 5})
    marks = ((NOW - timedelta(minutes=30), 10_000.0), *_marks(9_700, 9_710))
    ctx = _ctx(pol, value=9_700.0, equity_marks=marks)
    orders = [buy("B.US", 5, 1)]
    assert IntradayLossLimit().apply(orders, ctx) == (orders, [])


def test_marks_after_now_are_ignored():
    pol = policy(intraday_loss_limit={"max_loss": 0.02})
    future = ((NOW + timedelta(minutes=1), 50_000.0),)
    ctx = _ctx(pol, value=9_990.0, equity_marks=_marks(10_000) + future)
    assert loss_breach(ctx) is None


def test_the_breach_names_its_level():
    pol = policy(intraday_loss_limit={"max_loss": 0.02, "hard_loss": 0.05})
    soft = loss_breach(_ctx(pol, value=9_700.0, equity_marks=_marks(10_000)))
    hard = loss_breach(_ctx(pol, value=9_400.0, equity_marks=_marks(10_000)))
    assert soft is not None and soft.level == "soft" and soft.halt == "buys"
    assert hard is not None and hard.level == "hard" and hard.halt == "all"
    assert loss_breach(_ctx(pol, value=9_900.0, equity_marks=_marks(10_000))) is None


def test_a_hard_loss_with_flatten_closes_every_position_and_keeps_the_halt_on_buys():
    pol = policy(intraday_loss_limit={"hard_loss": 0.05, "flatten": True})
    ctx = _ctx(
        pol,
        value=9_000.0,
        positions={"A.US": 10, "C.US": -3},
        equity_marks=_marks(10_000),
    )
    ctx = replace(ctx, portfolio_id="pf_x")
    orders = [buy("B.US", 5, 1), sell("A.US", 4, 2)]
    kept, adj = IntradayLossLimit().apply(orders, ctx)
    assert orders[1] in kept  # the strategy's close
    assert all(o.ticker != "B.US" for o in kept)
    forced = [o for o in kept if o.client_id.startswith(NOW.date().isoformat())]
    assert {(o.ticker, o.side, o.quantity) for o in forced} == {
        ("A.US", "sell", 6.0),
        ("C.US", "buy", 3.0),
    }
    assert all(o.position_effect == "close" for o in forced)
    assert all(":1500" in o.client_id and "pf_x" in o.client_id for o in forced)
    assert {a.rule for a in adj} == {"intraday_loss_hard", INTRADAY_FLATTEN}
    breach = loss_breach(ctx)
    assert breach is not None and breach.halt == "buys"


def test_an_unmarked_holding_never_fakes_a_loss():
    pol = policy(intraday_loss_limit={"max_loss": 0.01})
    ctx = _ctx(pol, value=10_000.0, positions={"A.US": 10}, equity_marks=_marks(20_000))
    ctx = replace(ctx, prices={"B.US": 100.0})
    assert loss_breach(ctx) is None


def test_loss_settings_validate():
    with pytest.raises(ValidationError):
        IntradayLossLimitSettings(max_loss=1.5)
    with pytest.raises(ValidationError):
        IntradayLossLimitSettings(window_minutes=0)
    assert not IntradayLossLimitSettings().active
    assert IntradayLossLimitSettings(hard_loss=0.1).active


# ---- intraday drawdown ---------------------------------------------------------------


def test_drawdown_from_the_days_high_scales_opening_orders():
    pol = policy(intraday_drawdown={"schedule": [(0.01, 0.5), (0.03, 0.0)]})
    marks = _marks(10_000, 10_500, 10_300, step=60)  # the peak was hours ago
    ctx = _ctx(pol, value=10_350.0, positions={"A.US": 5}, equity_marks=marks)
    orders = [buy("B.US", 10, 1), sell("A.US", 5, 2)]
    kept, adj = IntradayDrawdown().apply(orders, ctx)
    assert [(o.ticker, o.quantity) for o in kept] == [("B.US", 5.0), ("A.US", 5.0)]
    assert [a.rule for a in adj] == ["intraday_drawdown"]


def test_drawdown_below_the_first_level_changes_nothing():
    pol = policy(intraday_drawdown={"schedule": [(0.05, 0.5)]})
    ctx = _ctx(pol, value=9_900.0, equity_marks=_marks(10_000))
    orders = [buy("B.US", 10, 1)]
    assert IntradayDrawdown().apply(orders, ctx) == (orders, [])


# ---- order rate ----------------------------------------------------------------------


def _scored(ticker: str, qty: float, i: int, score: float) -> Order:
    return replace(buy(ticker, qty, i), decision_context={"score": score})


def test_opening_orders_over_the_minute_cap_are_dropped_lowest_score_first():
    pol = policy(intraday_order_rate={"max_orders_per_minute": 3})
    sent = (NOW - timedelta(seconds=90), NOW - timedelta(seconds=20))  # one inside the minute
    ctx = _ctx(pol, positions={"A.US": 5}, sent_at=sent)
    orders = [
        _scored("B.US", 1, 1, 0.1),
        _scored("C.US", 1, 2, 0.9),
        sell("A.US", 5, 3),
        _scored("D.US", 1, 4, 0.5),
    ]
    kept, adj = IntradayOrderRate().apply(orders, ctx)
    # room 2: the close takes one slot, the best opener the other
    assert [o.ticker for o in kept] == ["C.US", "A.US"]
    assert sorted(a.ticker for a in adj) == ["B.US", "D.US"]
    assert all(a.rule == "intraday_order_rate" for a in adj)


def test_closes_go_out_even_when_the_cap_is_used_up():
    pol = policy(intraday_order_rate={"max_orders_per_minute": 1, "max_orders_per_day": 2})
    sent = tuple(NOW - timedelta(seconds=s) for s in (5, 10, 15))
    ctx = _ctx(pol, positions={"A.US": 5, "C.US": 1}, sent_at=sent)
    orders = [sell("A.US", 5, 1), sell("C.US", 1, 2), buy("B.US", 1, 3)]
    kept, adj = IntradayOrderRate().apply(orders, ctx)
    assert [o.ticker for o in kept] == ["A.US", "C.US"]
    assert [a.ticker for a in adj] == ["B.US"]


def test_the_day_cap_counts_only_orders_sent_before_now():
    pol = policy(intraday_order_rate={"max_orders_per_day": 2})
    sent = (NOW - timedelta(hours=2), NOW + timedelta(seconds=5))
    ctx = _ctx(pol, sent_at=sent)
    kept, adj = IntradayOrderRate().apply([buy("A.US", 1, 1), buy("B.US", 1, 2)], ctx)
    assert [o.ticker for o in kept] == ["A.US"] and len(adj) == 1


# ---- stale data ----------------------------------------------------------------------


def test_stale_or_missing_bars_block_opening_orders_only():
    pol = policy(intraday_stale_data={"max_bar_age_seconds": 120})
    ctx = _ctx(
        pol,
        positions={"C.US": 5},
        last_bar_at={"A.US": NOW - timedelta(seconds=60), "B.US": NOW - timedelta(seconds=300)},
    )
    orders = [buy("A.US", 1, 1), buy("B.US", 1, 2), buy("D.US", 1, 3), sell("C.US", 5, 4)]
    kept, adj = IntradayStaleData().apply(orders, ctx)
    assert [o.ticker for o in kept] == ["A.US", "C.US"]
    assert {a.ticker for a in adj} == {"B.US", "D.US"}
    assert all(a.rule == "intraday_stale_data" for a in adj)


def test_a_stale_stream_blocks_every_opening_order():
    pol = policy(intraday_stale_data={"max_bar_age_seconds": 120})
    ctx = _ctx(
        pol,
        positions={"C.US": 5},
        last_bar_at={"A.US": NOW},
        stream_stale=True,
    )
    kept, adj = IntradayStaleData().apply([buy("A.US", 1, 1), sell("C.US", 5, 2)], ctx)
    assert [o.ticker for o in kept] == ["C.US"]
    assert "stream" in adj[0].reason


# ---- settings merge ------------------------------------------------------------------


def test_overrides_can_only_tighten_the_intraday_rules():
    base = RuleSettings.model_validate(
        {
            "intraday_loss_limit": {"max_loss": 0.02, "window_minutes": 5, "hard_loss": 0.05},
            "intraday_drawdown": {"schedule": [(0.02, 0.5)]},
            "intraday_order_rate": {"max_orders_per_minute": 10},
            "intraday_stale_data": {"max_bar_age_seconds": 120},
        }
    )
    loose = {
        "intraday_loss_limit": {"max_loss": 0.1, "window_minutes": 1, "flatten": False},
        "intraday_drawdown": {"schedule": [(0.5, 1.0)]},
        "intraday_order_rate": {"max_orders_per_minute": 100, "max_orders_per_day": 50},
        "intraday_stale_data": {"max_bar_age_seconds": 900},
    }
    merged = tighter_rule_settings(base, loose)
    assert merged.intraday_loss_limit.max_loss == 0.02
    assert merged.intraday_loss_limit.window_minutes == 5
    assert merged.intraday_loss_limit.hard_loss == 0.05
    assert merged.intraday_drawdown.schedule == ((0.02, 0.5), (0.5, 0.5))
    assert merged.intraday_order_rate.max_orders_per_minute == 10
    assert merged.intraday_order_rate.max_orders_per_day == 50  # a new limit is tighter
    assert merged.intraday_stale_data.max_bar_age_seconds == 120
    tight = tighter_rule_settings(
        base, {"intraday_loss_limit": {"max_loss": 0.01, "flatten": True, "window_minutes": 15}}
    )
    assert tight.intraday_loss_limit.max_loss == 0.01
    assert tight.intraday_loss_limit.flatten is True
    assert tight.intraday_loss_limit.window_minutes == 15
