"""The cash-flow-adjusted equity curve the drawdown rules read: deposits
and withdrawals are taken out the same way as in TWR."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.insights.returns import Flow, twr
from stonks.production.risk_curve import flow_adjusted_curve
from stonks.production.rules.circuit_breaker import CircuitBreakerSettings, breaker_trips
from stonks.production.rules.drawdown_scaling import drawdown_scale

D1, D2, D3, D4 = date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5)


def _dd(curve: list[tuple[date, float]]) -> float:
    peak, deepest = 0.0, 0.0
    for _, v in curve:
        peak = max(peak, v)
        deepest = max(deepest, 1 - v / peak)
    return deepest


def test_no_flows_leaves_the_curve_as_is():
    points = [(D1, 100.0), (D2, 90.0), (D3, 95.0)]
    assert flow_adjusted_curve(points, []) == points


def test_a_deposit_after_a_loss_keeps_the_drawdown():
    # 10% loss, then 100 deposited: the book is worth 190 but still 10% under its peak
    points = [(D1, 100.0), (D2, 90.0), (D3, 190.0)]
    adjusted = flow_adjusted_curve(points, [Flow(D3, 100.0)])
    assert _dd(adjusted) == pytest.approx(0.10)
    assert adjusted[-1] == (D3, pytest.approx(190.0))  # in today's money


def test_a_withdrawal_is_not_a_loss():
    points = [(D1, 100.0), (D2, 110.0), (D3, 60.0)]
    adjusted = flow_adjusted_curve(points, [Flow(D3, -50.0)])
    assert _dd(adjusted) == pytest.approx(0.0)
    assert adjusted[-1][1] == pytest.approx(60.0)


def test_the_curve_follows_twr():
    points = [(D1, 100.0), (D2, 105.0), (D3, 300.0), (D4, 280.0)]
    flows = [Flow(D3, 200.0), Flow(D4, -10.0)]
    adjusted = flow_adjusted_curve(points, flows)
    assert adjusted[-1][1] / adjusted[0][1] - 1 == pytest.approx(twr(points, flows))


def test_flows_before_the_first_value_are_starting_capital():
    points = [(D2, 100.0), (D3, 90.0)]
    assert flow_adjusted_curve(points, [Flow(D1, 1_000.0), Flow(D2, 50.0)]) == points


def test_a_flow_after_the_last_value_rescales_to_todays_money():
    # the live value of D4 (190) already holds the D4 deposit: compared with
    # the adjusted last point it shows no loss on the day
    points = [(D1, 100.0), (D2, 90.0)]
    adjusted = flow_adjusted_curve(points, [Flow(D4, 100.0)], as_of=D4)
    assert adjusted[-1][1] == pytest.approx(190.0)
    assert _dd([*adjusted, (D4, 190.0)]) == pytest.approx(0.10)


def test_a_flow_after_as_of_is_ignored():
    points = [(D1, 100.0), (D2, 90.0)]
    assert flow_adjusted_curve(points, [Flow(D4, 100.0)], as_of=D3) == points


def test_a_book_emptied_by_a_withdrawal_does_not_crash():
    points = [(D1, 100.0), (D2, 0.0), (D3, 50.0)]
    adjusted = flow_adjusted_curve(points, [Flow(D2, -100.0), Flow(D3, 50.0)])
    assert [d for d, _ in adjusted] == [D1, D2, D3]
    assert _dd(adjusted) == pytest.approx(0.0)


def test_the_breaker_does_not_trip_on_a_withdrawal():
    settings = CircuitBreakerSettings(max_month_loss=0.06, max_drawdown_halt=0.2)
    points = [(D1, 10_000.0), (D2, 10_100.0), (D3, 5_100.0)]
    assert breaker_trips(points, D3, settings)  # the raw curve trips
    adjusted = flow_adjusted_curve(points, [Flow(D3, -5_000.0)])
    assert breaker_trips(adjusted, D3, settings) == []


def test_drawdown_scaling_sees_a_loss_hidden_by_a_deposit():
    points = [(D1, 100.0), (D2, 90.0), (D3, 190.0)]
    schedule = [(0.05, 0.5)]
    raw_scale, _ = drawdown_scale([v for _, v in points], schedule)
    assert raw_scale == 1.0
    adjusted = flow_adjusted_curve(points, [Flow(D3, 100.0)])
    scale, dd = drawdown_scale([v for _, v in adjusted], schedule)
    assert (scale, dd) == (0.5, pytest.approx(0.10))
