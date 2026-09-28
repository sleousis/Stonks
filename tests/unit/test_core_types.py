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


@pytest.mark.parametrize("quantity", [float("nan"), float("inf")])
def test_order_rejects_a_quantity_that_is_not_finite(quantity):
    # NaN passes a "<= 0" check, and an order of NaN shares would turn the
    # broker's cash and positions into NaN
    with pytest.raises(ValueError, match="quantity"):
        Order(client_id="x", ticker="AAPL.US", side="buy", quantity=quantity)


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


def test_portfolio_reports_held_tickers_without_a_mark():
    # DS-13: a held ticker with no price is not silently a total loss
    p = Portfolio(cash=0.0, positions={"AAPL.US": 1.0, "NEW.US": 10.0})
    prices = {"AAPL.US": 50.0, "MSFT.US": 1.0}
    assert p.unmarked(prices) == ["NEW.US"]
    assert p.unmarked({"AAPL.US": 50.0, "NEW.US": 2.0}) == []
    # a NaN mark counts as missing too
    assert p.unmarked({"AAPL.US": 50.0, "NEW.US": float("nan")}) == ["NEW.US"]


def test_features_dict_access_and_get_default():
    f = Features(values={"r_20": 0.05, "vol": 0.2})
    assert f.get("r_20") == 0.05
    assert f.get("unknown") is None
    assert f.get("unknown", default=0.0) == 0.0


# ---- live broker fields (roadmap 19.1) ------------------------------------------


def test_order_live_fields_default_to_the_paper_behaviour():
    order = Order(client_id="x", ticker="AAPL.US", side="buy", quantity=1.0)
    assert order.stop_price is None
    assert order.time_in_force is None
    assert order.outside_rth is False


def test_stop_limit_order_carries_stop_and_limit():
    order = Order(
        client_id="x",
        ticker="AAPL.US",
        side="sell",
        quantity=1.0,
        order_type="stop_limit",
        limit_price=95.0,
        stop_price=96.0,
        time_in_force="gtc",
    )
    assert order.stop_price == 96.0
    assert order.time_in_force == "gtc"


@pytest.mark.parametrize("field", ["stop_price", "limit_price"])
@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf")])
def test_order_prices_must_be_positive_and_finite(field, value):
    kwargs = {"order_type": "limit", "limit_price": 10.0, field: value}
    with pytest.raises(ValueError, match=field):
        Order(client_id="x", ticker="AAPL.US", side="buy", quantity=1.0, **kwargs)


def test_order_rejects_an_unknown_time_in_force():
    with pytest.raises(ValueError, match="time_in_force"):
        Order(
            client_id="x",
            ticker="AAPL.US",
            side="buy",
            quantity=1.0,
            time_in_force="week",  # type: ignore[arg-type]
        )


def test_fill_execution_id_and_fee_currency_are_optional():
    fill = Fill(
        order_client_id="x",
        ticker="AAPL.US",
        quantity=1.0,
        price=10.0,
        fee=0.5,
        filled_at=_now(),
        side="buy",
    )
    assert fill.broker_exec_id is None
    assert fill.fee_currency is None
    tagged = dataclasses.replace(fill, broker_exec_id="0001.01", fee_currency="USD")
    assert tagged.broker_exec_id == "0001.01"
