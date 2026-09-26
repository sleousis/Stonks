"""Unit tests for applying corporate actions to a simulated portfolio."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from stonks.backtest.corporate_actions import (
    CorporateActionSchedule,
    adjust_orders_for_split,
    apply_to_portfolio,
)
from stonks.core.corporate_actions import CorporateActions, Dividend, Split
from stonks.core.types import Order, Portfolio

AS_OF = datetime(2024, 6, 10)


def test_split_multiplies_quantity_and_leaves_cash():
    p = Portfolio(cash=5.0, positions={"X": 10.0})
    rec = apply_to_portfolio(p, Split("X", date(2024, 6, 10), 4.0), AS_OF)
    assert p.positions == {"X": 40.0}
    assert p.cash == 5.0
    assert (rec.kind, rec.quantity_before, rec.quantity_after, rec.cash_delta) == (
        "split",
        10.0,
        40.0,
        0.0,
    )


def test_dividend_credits_cash_net_of_withholding():
    p = Portfolio(cash=0.0, positions={"X": 10.0})
    rec = apply_to_portfolio(p, Dividend("X", date(2024, 6, 10), 0.5), AS_OF, withholding_rate=0.3)
    assert p.cash == pytest.approx(3.5)
    assert rec.cash_delta == pytest.approx(3.5)
    assert p.positions == {"X": 10.0}


def test_short_position_pays_the_dividend():
    p = Portfolio(cash=100.0, positions={"X": -10.0})
    apply_to_portfolio(p, Dividend("X", date(2024, 6, 10), 1.0), AS_OF)
    assert p.cash == pytest.approx(90.0)


def test_no_position_no_record():
    p = Portfolio(cash=1.0)
    assert apply_to_portfolio(p, Split("X", date(2024, 6, 10), 2.0), AS_OF) is None
    assert apply_to_portfolio(p, Dividend("X", date(2024, 6, 10), 2.0), AS_OF) is None
    assert p == Portfolio(cash=1.0)


def test_schedule_releases_each_event_once_on_or_after_ex_date():
    actions = CorporateActions.from_events(
        [
            Dividend("X", date(2024, 6, 12), 1.0),
            Split("X", date(2024, 6, 10), 2.0),
            Dividend("X", date(2024, 6, 10), 0.1),
        ]
    )
    schedule = CorporateActionSchedule(actions)
    assert schedule.due("X", date(2024, 6, 7)) == []
    # a gap in bars: both events on/before the 11th come due, split first
    assert schedule.due("X", date(2024, 6, 11)) == [
        Split("X", date(2024, 6, 10), 2.0),
        Dividend("X", date(2024, 6, 10), 0.1),
    ]
    assert schedule.due("X", date(2024, 6, 11)) == []
    assert schedule.due("X", date(2024, 6, 20)) == [Dividend("X", date(2024, 6, 12), 1.0)]
    assert schedule.due("Y", date(2024, 6, 20)) == []


def test_queued_orders_for_the_split_ticker_are_rescaled():
    orders = [
        Order(client_id="a", ticker="X", side="buy", quantity=3.0),
        Order(
            client_id="b",
            ticker="X",
            side="sell",
            quantity=2.0,
            order_type="limit",
            limit_price=100.0,
        ),
        Order(client_id="c", ticker="Y", side="buy", quantity=1.0),
    ]
    out = adjust_orders_for_split(orders, Split("X", date(2024, 6, 10), 10.0))
    assert [(o.client_id, o.quantity, o.limit_price) for o in out] == [
        ("a", 30.0, None),
        ("b", 20.0, 10.0),
        ("c", 1.0, None),
    ]
