"""Unit tests for the cross-block value types: Order, Fill, Portfolio, Features."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from stonks.core.types import Features, Fill, Order, Portfolio


def _now() -> datetime:
    return datetime.now(UTC)


def test_order_is_frozen():
    order = Order(
        client_id="t1:s1:AAPL:buy",
        ticker="AAPL.US",
        side="buy",
        quantity=10.0,
        order_type="market",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        order.quantity = 20.0  # type: ignore[misc]


def test_order_requires_limit_price_when_order_type_is_limit():
    with pytest.raises(ValueError, match="limit_price"):
        Order(
            client_id="x",
            ticker="AAPL.US",
            side="buy",
            quantity=1.0,
            order_type="limit",
            limit_price=None,
        )


def test_order_rejects_non_positive_quantity():
    with pytest.raises(ValueError, match="quantity"):
        Order(client_id="x", ticker="AAPL.US", side="buy", quantity=0.0)
    with pytest.raises(ValueError, match="quantity"):
        Order(client_id="x", ticker="AAPL.US", side="buy", quantity=-1.0)


def test_fill_construction_and_signed_quantity():
    f = Fill(
        order_client_id="x",
        ticker="AAPL.US",
        quantity=5.0,
        price=100.0,
        fee=1.0,
        filled_at=_now(),
        side="buy",
    )
    assert f.signed_quantity == 5.0
    f2 = Fill(
        order_client_id="x",
        ticker="AAPL.US",
        quantity=5.0,
        price=100.0,
        fee=1.0,
        filled_at=_now(),
        side="sell",
    )
    assert f2.signed_quantity == -5.0


def test_portfolio_apply_buy_updates_cash_and_positions():
    p = Portfolio(cash=10_000.0, positions={})
    f = Fill(
        order_client_id="x",
        ticker="AAPL.US",
        quantity=10.0,
        price=200.0,
        fee=1.0,
        filled_at=_now(),
        side="buy",
    )
    p.apply_fill(f)
    assert p.positions == {"AAPL.US": 10.0}
    assert p.cash == pytest.approx(10_000.0 - 10 * 200 - 1.0)


def test_portfolio_apply_sell_reduces_position_and_adds_cash():
    p = Portfolio(cash=0.0, positions={"AAPL.US": 10.0})
    f = Fill(
        order_client_id="x",
        ticker="AAPL.US",
        quantity=4.0,
        price=250.0,
        fee=0.5,
        filled_at=_now(),
        side="sell",
    )
    p.apply_fill(f)
    assert p.positions == {"AAPL.US": 6.0}
    assert p.cash == pytest.approx(4 * 250 - 0.5)


def test_portfolio_total_value_sums_cash_and_mark_to_market():
    p = Portfolio(cash=500.0, positions={"AAPL.US": 2.0, "MSFT.US": 5.0})
    total = p.total_value(prices={"AAPL.US": 100.0, "MSFT.US": 50.0})
    assert total == pytest.approx(500 + 2 * 100 + 5 * 50)


def test_portfolio_total_value_treats_missing_price_as_zero():
    p = Portfolio(cash=0.0, positions={"AAPL.US": 1.0, "UNKNOWN": 10.0})
    total = p.total_value(prices={"AAPL.US": 50.0})
    assert total == pytest.approx(50.0)


def test_features_dict_access_and_get_default():
    f = Features(values={"r_20": 0.05, "vol": 0.2})
    assert f.get("r_20") == 0.05
    assert f.get("unknown") is None
    assert f.get("unknown", default=0.0) == 0.0
