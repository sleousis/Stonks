"""Circuit breaker and operational halt risk rules (BL-28, W3.2)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from stonks.core.types import Portfolio
from stonks.production.risk import apply_risk
from stonks.production.rules.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerSettings,
    breaker_trips,
    next_month_start,
)
from stonks.production.rules.operational_halt import OperationalHalt
from stonks.production.rules.settings import RuleSettings, tighter_rule_settings
from tests.fixtures.risk_rules import bars, buy, context, policy, sell

ON = CircuitBreakerSettings(max_month_loss=0.06, max_week_loss=0.04, max_drawdown_halt=0.20)


def _days(start: date, values: list[float]) -> list[tuple[date, float]]:
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


def test_off_by_default():
    assert not CircuitBreakerSettings().active
    assert not CircuitBreaker().enabled(policy())
    assert not OperationalHalt().enabled(policy())


def test_a_six_percent_month_loss_trips_until_the_month_ends():
    curve = _days(date(2025, 6, 2), [100.0, 99.0, 93.9, 97.0])
    trips = breaker_trips(curve, date(2025, 6, 5), ON.model_copy(update={"max_week_loss": None}))
    assert [t.kind for t in trips] == ["month_loss"]
    assert trips[0].expires_on == date(2025, 7, 1)  # rest of the month, even after recovery


def test_the_month_is_measured_from_its_first_snapshot():
    # May's losses do not count against June.
    curve = [(date(2025, 5, 30), 120.0), *_days(date(2025, 6, 2), [100.0, 96.0])]
    only_month = ON.model_copy(update={"max_week_loss": None, "max_drawdown_halt": None})
    assert breaker_trips(curve, date(2025, 6, 3), only_month) == []


def test_a_four_percent_loss_over_five_sessions_trips_the_week_halt():
    curve = _days(date(2025, 6, 2), [100.0, 101.0, 100.0, 99.0, 98.0, 97.0, 96.5])
    only_week = ON.model_copy(update={"max_month_loss": None, "max_drawdown_halt": None})
    [trip] = breaker_trips(curve, date(2025, 6, 8), only_week)
    assert trip.kind == "week_loss"
    assert "4.46%" in trip.reason


def test_cooldown_none_only_halts_while_the_loss_holds():
    curve = _days(date(2025, 6, 2), [100.0, 93.0, 99.0])
    live = ON.model_copy(update={"cooldown": "none", "max_week_loss": None})
    assert breaker_trips(curve, date(2025, 6, 4), live) == []
    [trip] = breaker_trips(curve[:2], date(2025, 6, 3), live)
    assert trip.expires_on == date(2025, 6, 4)


def test_the_drawdown_halt_latches_after_a_recovery():
    curve = [(date(2025, 1, 2), 100.0), (date(2025, 2, 3), 79.0), (date(2025, 3, 3), 120.0)]
    only_dd = ON.model_copy(update={"max_month_loss": None, "max_week_loss": None})
    [trip] = breaker_trips(curve, date(2025, 3, 3), only_dd)
    assert trip.kind == "drawdown" and trip.expires_on is None
    # after a reset the replay starts again from the reset day
    assert breaker_trips(curve, date(2025, 3, 3), only_dd, drawdown_since=date(2025, 2, 4)) == []


def test_next_month_start_rolls_the_year():
    assert next_month_start(date(2025, 12, 31)) == date(2026, 1, 1)


def test_the_rule_drops_buys_and_lets_sells_through():
    pol = policy(circuit_breaker=ON.model_dump())
    pf = Portfolio(cash=4_000.0, positions={"A.US": 10.0})
    curve = _days(date(2025, 6, 2), [10_000.0, 9_800.0])
    ctx = context(
        pf, {"A.US": 500.0, "B.US": 50.0}, pol, equity_curve=curve, as_of=date(2025, 6, 4)
    )
    # today's value 4000 + 5000 = 9000: a 10% month loss
    result = apply_risk(
        [buy("B.US", 10), sell("A.US", 5)], pf, ctx.prices, ctx.asset_classes, pol, context=ctx
    )
    assert [(o.ticker, o.side) for o in result.orders] == [("A.US", "sell")]
    assert {a.rule for a in result.adjustments} == {"circuit_breaker"}


def test_the_operational_halt_drops_buys_when_every_feed_is_stale():
    pol = policy(operational_halt={"max_bar_age_days": 4})
    pf = Portfolio(cash=10_000.0, positions={"A.US": 1.0})
    stale = {"A.US": bars([10.0] * 30, end=date(2025, 6, 20))}
    ctx = context(pf, {"A.US": 10.0}, pol, history=stale, as_of=date(2025, 6, 30))
    orders = [buy("A.US", 5), sell("A.US", 1)]
    result = apply_risk(orders, pf, ctx.prices, ctx.asset_classes, pol, context=ctx)
    assert [o.side for o in result.orders] == ["sell"]
    fresh = {"A.US": bars([10.0] * 30, end=date(2025, 6, 27))}
    ctx = context(pf, {"A.US": 10.0}, pol, history=fresh, as_of=date(2025, 6, 30))
    result = apply_risk(orders, pf, ctx.prices, ctx.asset_classes, pol, context=ctx)
    assert len(result.orders) == 2


@pytest.mark.parametrize(
    ("override", "field", "expected"),
    [
        ({"max_month_loss": 0.03}, "max_month_loss", 0.03),
        ({"max_month_loss": 0.10}, "max_month_loss", 0.06),
        ({"cooldown": "none"}, "cooldown", "rest_of_month"),
    ],
)
def test_overrides_only_tighten(override, field, expected):
    base = RuleSettings(circuit_breaker=ON)
    merged = tighter_rule_settings(base, {"circuit_breaker": override})
    assert getattr(merged.circuit_breaker, field) == expected
