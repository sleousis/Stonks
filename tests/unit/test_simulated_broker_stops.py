"""SimulatedBroker resting stops (roadmap 19.10): a good till cancelled stop
rests at the broker and fills when a later bar's range reaches it, at the
stop or at a gapped open, never selling more than the book holds."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.backtest.fills import triggered_price
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Order, Portfolio

DAY1, DAY2 = date(2026, 3, 17), date(2026, 3, 18)


def _stop(cid="s1", side="sell", qty=10.0, stop=90.0, ticker="A.US", tif="gtc") -> Order:
    return Order(
        client_id=cid,
        ticker=ticker,
        side=side,
        quantity=qty,
        order_type="stop",
        stop_price=stop,
        time_in_force=tif,
        position_effect="close",
    )


def _broker(positions=None, cash=10_000.0) -> SimulatedBroker:
    return SimulatedBroker(Portfolio(cash=cash, positions={"A.US": 10.0} if positions is None else dict(positions)))


def test_a_gtc_stop_rests_instead_of_filling_at_once():
    b = _broker()
    b.set_prices({"A.US": 100.0}, DAY1)
    assert b.place_order(_stop()) is None
    assert [o.client_id for o in b.resting_orders()] == ["s1"]
    assert b.fetch_portfolio().positions == {"A.US": 10.0}
    assert b.place_order(_stop()) is None  # idempotent
    assert len(b.resting_orders()) == 1


def test_it_fills_when_the_bar_reaches_the_stop():
    b = _broker()
    b.set_prices({"A.US": 100.0}, DAY1)
    b.place_order(_stop())
    b.set_prices({"A.US": 95.0}, DAY2, highs={"A.US": 96.0}, lows={"A.US": 91.0})
    assert b.trigger_resting() == []
    b.set_prices({"A.US": 95.0}, DAY2, highs={"A.US": 96.0}, lows={"A.US": 89.0})
    [fill] = b.trigger_resting()
    assert (fill.order_client_id, fill.side, fill.quantity, fill.price) == ("s1", "sell", 10, 90)
    assert b.fetch_portfolio().positions.get("A.US", 0.0) == 0.0
    assert b.resting_orders() == ()
    assert b.trigger_resting() == []


def test_a_gap_through_the_stop_fills_at_the_open():
    b = _broker()
    b.set_prices({"A.US": 100.0}, DAY1)
    b.place_order(_stop())
    b.set_prices({"A.US": 80.0}, DAY2, highs={"A.US": 82.0}, lows={"A.US": 78.0})
    [fill] = b.trigger_resting()
    assert fill.price == 80.0


def test_a_stop_never_sells_more_than_is_held():
    b = _broker(positions={"A.US": 4.0})
    b.set_prices({"A.US": 100.0}, DAY1)
    b.place_order(_stop(qty=10.0))
    b.set_prices({"A.US": 85.0}, DAY2, highs={"A.US": 86.0}, lows={"A.US": 84.0})
    [fill] = b.trigger_resting()
    assert fill.quantity == 4.0
    flat = _broker(positions={})
    flat.set_prices({"A.US": 100.0}, DAY1)
    flat.place_order(_stop())
    flat.set_prices({"A.US": 85.0}, DAY2, highs={"A.US": 86.0}, lows={"A.US": 84.0})
    assert flat.trigger_resting() == [] and flat.resting_orders() == ()


def test_a_buy_stop_covers_a_short_when_the_price_rises():
    b = _broker(positions={"A.US": -5.0})
    b.set_prices({"A.US": 100.0}, DAY1)
    b.place_order(_stop(side="buy", qty=5.0, stop=110.0))
    b.set_prices({"A.US": 105.0}, DAY2, highs={"A.US": 112.0}, lows={"A.US": 104.0})
    [fill] = b.trigger_resting()
    assert (fill.side, fill.quantity, fill.price) == ("buy", 5.0, 110.0)
    assert b.fetch_portfolio().positions.get("A.US", 0.0) == 0.0


def test_cancel_removes_a_resting_stop():
    b = _broker()
    b.set_prices({"A.US": 100.0}, DAY1)
    b.place_order(_stop())
    assert b.cancel_order("s1") is True
    assert b.cancel_order("s1") is False
    b.set_prices({"A.US": 80.0}, DAY2, highs={"A.US": 82.0}, lows={"A.US": 78.0})
    assert b.trigger_resting() == []


def test_a_day_stop_still_fills_through_the_fill_model_at_once():
    b = _broker()
    b.set_prices({"A.US": 100.0}, DAY1)
    assert b.place_order(_stop(tif="day")) is not None  # the legacy immediate model


@pytest.mark.parametrize(
    ("side", "stop", "bar", "expected"),
    [
        ("sell", 90.0, (95.0, 96.0, 91.0), None),
        ("sell", 90.0, (95.0, 96.0, 90.0), 90.0),
        ("sell", 90.0, (85.0, 86.0, 84.0), 85.0),
        ("buy", 110.0, (105.0, 109.0, 100.0), None),
        ("buy", 110.0, (115.0, 116.0, 114.0), 115.0),
    ],
)
def test_triggered_price(side, stop, bar, expected):
    o, h, low = bar
    assert triggered_price(_stop(side=side, stop=stop), o, h, low) == expected
