"""Paper books fill at the next session's open, like the backtest (P21)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from stonks.backtest.fills import FillModelSettings
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.corporate_actions import CorporateActions, Split
from stonks.core.types import Order, Portfolio
from stonks.production.paper_fills import OpenBar, WorkingOrder, fill_at_next_open

MON, TUE, WED = date(2026, 3, 16), date(2026, 3, 17), date(2026, 3, 18)


def _broker(cash: float = 10_000.0, **fill) -> SimulatedBroker:
    return SimulatedBroker(
        Portfolio(cash=cash), fill_model=FillModelSettings(**fill).build() if fill else None
    )


def _buy(qty: float = 10.0, cid: str = "b1", ticker: str = "A.US") -> Order:
    return Order(client_id=cid, ticker=ticker, side="buy", quantity=qty)


def _working(order: Order, day: date = MON) -> WorkingOrder:
    return WorkingOrder(order=order, decided_on=day)


def test_an_order_fills_at_the_open_of_the_next_session():
    bars = {TUE: {"A.US": OpenBar(open=101.0, high=103.0, low=99.0, volume=1e6)}}
    [out] = fill_at_next_open(_broker(), [_working(_buy())], bars, as_of=TUE)
    assert out.status == "filled"
    assert out.fill is not None
    assert out.fill.price == 101.0
    assert out.fill.quantity == 10.0
    assert out.fill.filled_at == datetime(2026, 3, 17, tzinfo=UTC)
    assert out.arrival == 101.0
    assert out.bar_day == TUE


def test_an_order_decided_today_keeps_working():
    bars = {TUE: {"A.US": OpenBar(open=101.0)}}
    assert fill_at_next_open(_broker(), [_working(_buy(), TUE)], bars, as_of=TUE) == []


def test_the_participation_cap_fills_part_and_the_next_decision_replaces_the_rest():
    bars = {TUE: {"A.US": OpenBar(open=100.0, volume=40.0)}}
    broker = _broker(max_participation=0.1)
    [out] = fill_at_next_open(broker, [_working(_buy())], bars, as_of=TUE)
    assert out.fill is not None
    assert out.fill.quantity == pytest.approx(4.0)
    assert out.status == "cancelled"
    assert out.reason is not None and "replaced by the next decision" in out.reason


def test_the_gap_guard_expires_an_order_after_a_long_halt():
    later = date(2026, 3, 30)
    bars = {later: {"A.US": OpenBar(open=100.0, volume=1e6)}}
    broker = _broker(max_gap_days=7.0)
    [out] = fill_at_next_open(broker, [_working(_buy())], bars, as_of=later)
    assert out.fill is None
    assert out.status == "expired"


def test_only_the_first_bar_after_the_decision_can_fill():
    bars = {
        TUE: {"A.US": OpenBar(open=100.0, volume=0.0)},
        WED: {"A.US": OpenBar(open=90.0, volume=1e6)},
    }
    broker = _broker(max_participation=0.1)
    [out] = fill_at_next_open(broker, [_working(_buy())], bars, as_of=WED)
    assert out.fill is None
    assert out.status == "cancelled"


def test_no_bar_since_the_decision_cancels_the_order():
    bars = {TUE: {"B.US": OpenBar(open=100.0)}}
    [out] = fill_at_next_open(_broker(), [_working(_buy())], bars, as_of=TUE)
    assert out.fill is None
    assert out.status == "cancelled"
    assert out.bar_day is None


def test_a_buy_scaled_to_cash_is_recorded_as_what_traded():
    bars = {TUE: {"A.US": OpenBar(open=100.0)}}
    [out] = fill_at_next_open(_broker(cash=500.0), [_working(_buy())], bars, as_of=TUE)
    assert out.fill is not None
    assert out.fill.quantity == pytest.approx(5.0)
    assert out.status == "filled"
    assert out.order.quantity == pytest.approx(5.0)
    assert out.reason == "filled 5 of 10 requested"


def test_sells_fill_before_buys_in_the_order_given():
    broker = SimulatedBroker(Portfolio(cash=0.0, positions={"A.US": 10.0}))
    sell = Order(client_id="s1", ticker="A.US", side="sell", quantity=10.0)
    buy = _buy(qty=5.0, cid="b1", ticker="B.US")
    bars = {TUE: {"A.US": OpenBar(open=100.0), "B.US": OpenBar(open=100.0)}}
    outs = fill_at_next_open(broker, [_working(sell), _working(buy)], bars, as_of=TUE)
    assert [o.status for o in outs] == ["filled", "filled"]
    assert broker.fetch_portfolio().positions == {"B.US": 5.0}


def test_a_split_between_decision_and_fill_rescales_the_order():
    actions = CorporateActions.from_events([Split(ticker="A.US", ex_date=TUE, ratio=2.0)])
    broker = SimulatedBroker(Portfolio(cash=0.0, positions={"A.US": 20.0}))
    sell = Order(client_id="s1", ticker="A.US", side="sell", quantity=10.0)
    bars = {TUE: {"A.US": OpenBar(open=50.0)}}
    [out] = fill_at_next_open(broker, [_working(sell)], bars, as_of=TUE, actions=actions)
    assert out.fill is not None
    assert out.fill.quantity == pytest.approx(20.0)
    assert out.fill.price == 50.0
