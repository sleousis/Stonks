"""Unit tests for the BuyAndHold reference strategy.

This is a rule-based strategy — no ML model, no fit. It declares a target
ticker and an allocation fraction; the first time it runs with cash available
it emits a single buy order, then holds indefinitely.
"""

from __future__ import annotations

from datetime import date

from stonks.core.types import Portfolio
from stonks.strategies.examples.buy_and_hold import BuyAndHold


def test_parameter_spec_lists_ticker_and_allocation():
    names = {spec.name for spec in BuyAndHold.parameter_spec()}
    assert {"ticker", "allocation"} <= names


def test_estimate_return_prefers_target_ticker():
    s = BuyAndHold({"ticker": "AAPL.US", "allocation": 1.0})
    assert s.estimate_return("AAPL.US", date(2026, 4, 1), lake=None) is not None
    assert s.estimate_return("MSFT.US", date(2026, 4, 1), lake=None) is None


def test_decide_emits_buy_when_not_holding_target():
    s = BuyAndHold({"ticker": "AAPL.US", "allocation": 1.0})
    portfolio = Portfolio(cash=10_000.0, positions={})
    prices = {"AAPL.US": 200.0}
    picks = [(1.0, "AAPL.US")]
    orders = s.decide(picks, portfolio, prices, as_of=date(2026, 4, 1))
    assert len(orders) == 1
    o = orders[0]
    assert o.side == "buy"
    assert o.ticker == "AAPL.US"
    assert o.quantity == 50.0  # 10_000 / 200 = 50 shares


def test_decide_emits_nothing_when_already_holding():
    s = BuyAndHold({"ticker": "AAPL.US", "allocation": 1.0})
    portfolio = Portfolio(cash=1_000.0, positions={"AAPL.US": 10.0})
    prices = {"AAPL.US": 200.0}
    picks = [(1.0, "AAPL.US")]
    orders = s.decide(picks, portfolio, prices, as_of=date(2026, 4, 1))
    assert orders == []


def test_allocation_fraction_respected():
    s = BuyAndHold({"ticker": "AAPL.US", "allocation": 0.25})
    portfolio = Portfolio(cash=10_000.0, positions={})
    prices = {"AAPL.US": 100.0}
    orders = s.decide([(1.0, "AAPL.US")], portfolio, prices, as_of=date(2026, 4, 1))
    assert orders[0].quantity == 25.0  # 10_000 * 0.25 / 100
