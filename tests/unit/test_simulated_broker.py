"""Unit tests for SimulatedBroker — the in-memory broker used by both backtest
and dry-run production tick.
"""

from __future__ import annotations

from datetime import date

import pytest

from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Order, Portfolio


@pytest.fixture
def broker():
    portfolio = Portfolio(cash=10_000.0, positions={})
    return SimulatedBroker(portfolio=portfolio, slippage_bps=0.0, fee_per_trade=0.0)


def _order(client_id: str, ticker="AAPL.US", side="buy", qty=10.0, order_type="market"):
    return Order(
        client_id=client_id,
        ticker=ticker,
        side=side,
        quantity=qty,
        order_type=order_type,
    )


def test_fetch_portfolio_returns_seeded(broker):
    p = broker.fetch_portfolio()
    assert p.cash == 10_000.0
    assert p.positions == {}


def test_place_order_market_fills_at_current_price(broker):
    broker.set_prices({"AAPL.US": 200.0}, as_of=date(2026, 4, 1))
    fill = broker.place_order(_order("o1"))
    assert fill is not None
    assert fill.ticker == "AAPL.US"
    assert fill.quantity == 10.0
    assert fill.price == 200.0
    # portfolio updated
    p = broker.fetch_portfolio()
    assert p.cash == 10_000.0 - 10 * 200
    assert p.positions == {"AAPL.US": 10.0}


def test_place_order_is_idempotent_on_client_id(broker):
    broker.set_prices({"AAPL.US": 200.0}, as_of=date(2026, 4, 1))
    f1 = broker.place_order(_order("dup"))
    f2 = broker.place_order(_order("dup"))  # re-submission
    # same fill returned, no duplicate impact
    assert f1 == f2
    p = broker.fetch_portfolio()
    assert p.positions == {"AAPL.US": 10.0}
    assert p.cash == 10_000.0 - 10 * 200


def test_place_order_rejects_if_insufficient_cash(broker):
    broker.set_prices({"AAPL.US": 200.0}, as_of=date(2026, 4, 1))
    fill = broker.place_order(_order("too_big", qty=1_000.0))  # 200k > 10k cash
    assert fill is None
    p = broker.fetch_portfolio()
    assert p.positions == {}
    assert p.cash == 10_000.0


def test_place_order_slippage_increases_buy_price(broker):
    broker = SimulatedBroker(
        portfolio=Portfolio(cash=10_000.0, positions={}),
        slippage_bps=50.0,  # 0.5%
        fee_per_trade=0.0,
    )
    broker.set_prices({"AAPL.US": 200.0}, as_of=date(2026, 4, 1))
    fill = broker.place_order(_order("buy1", side="buy", qty=10.0))
    assert fill.price == pytest.approx(200.0 * 1.005)


def test_place_order_slippage_decreases_sell_price(broker):
    broker = SimulatedBroker(
        portfolio=Portfolio(cash=0.0, positions={"AAPL.US": 10.0}),
        slippage_bps=50.0,
        fee_per_trade=0.0,
    )
    broker.set_prices({"AAPL.US": 200.0}, as_of=date(2026, 4, 1))
    fill = broker.place_order(_order("sell1", side="sell", qty=5.0))
    assert fill.price == pytest.approx(200.0 * 0.995)


def test_reconcile_returns_all_recorded_fills(broker):
    broker.set_prices({"AAPL.US": 200.0, "MSFT.US": 100.0}, as_of=date(2026, 4, 1))
    broker.place_order(_order("o1", ticker="AAPL.US", qty=10.0))
    broker.place_order(_order("o2", ticker="MSFT.US", qty=20.0))
    fills = broker.reconcile()
    assert len(fills) == 2
    assert {f.ticker for f in fills} == {"AAPL.US", "MSFT.US"}
