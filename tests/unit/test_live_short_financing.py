"""Short book financing at a real broker (roadmap 19.13): the borrow fee of
each of the book's own shorts, at the broker's borrow rate, for the days
since the last accrual."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.clock import FixedClock
from stonks.execution.borrow import BorrowQuote, FlatBorrow
from stonks.execution.brokers.ibkr.borrow import IbkrBorrowSource
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.execution.brokers.ibkr.client import IbShortability
from stonks.production.financing import live_short_financing
from tests.fakes.ib_gateway import AAPL, T0, FakeIbGateway

DAY = T0.date()


def test_charges_each_short_at_its_fee_for_the_days():
    borrow = FlatBorrow(overrides={"AAPL.US": BorrowQuote("easy", 0.036, None)})
    events = live_short_financing(
        {"AAPL.US": -100.0, "MSFT.US": 50.0},
        {"AAPL.US": 200.0, "MSFT.US": 400.0},
        DAY,
        since=date(2026, 9, 25),
        borrow=borrow,
    )
    assert [(e.ticker, e.kind, e.days) for e in events] == [("AAPL.US", "borrow_fee", 3)]
    # 100 x 200 x 3.6% x 3 / 360
    assert events[0].amount == pytest.approx(-6.0)


def test_no_start_or_no_new_day_charges_nothing():
    borrow = FlatBorrow()
    assert live_short_financing({"A.US": -1.0}, {"A.US": 1.0}, DAY, since=None, borrow=borrow) == []
    assert live_short_financing({"A.US": -1.0}, {"A.US": 1.0}, DAY, since=DAY, borrow=borrow) == []


def test_a_short_without_a_quote_or_price_is_skipped():
    borrow = FlatBorrow(overrides={"A.US": BorrowQuote("none", 0.0, 0.0)})
    events = live_short_financing(
        {"A.US": -1.0, "B.US": -5.0}, {"A.US": 10.0}, DAY, since=date(2026, 9, 27), borrow=borrow
    )
    assert events == []


def test_the_fee_comes_from_ibkrs_borrow_rates():
    gw = FakeIbGateway()
    gw.shortable_data[AAPL.contract.con_id] = IbShortability(AAPL.contract.con_id, 3.0, 1e6)
    broker = IbkrBroker(gw, mode="paper", account_type="margin", allow_short=True,
                        clock=FixedClock(T0))  # fmt: skip
    rates = FlatBorrow(overrides={"AAPL.US": BorrowQuote("easy", 0.012, 1e7)})
    source = IbkrBorrowSource(broker, fees=rates)
    events = live_short_financing(
        {"AAPL.US": -10.0}, {"AAPL.US": 300.0}, DAY, since=date(2026, 9, 27), borrow=source
    )
    assert events[0].amount == pytest.approx(-10 * 300 * 0.012 / 360)
