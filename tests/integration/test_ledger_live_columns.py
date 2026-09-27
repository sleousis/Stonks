"""The tick's ledger writes carry the live order and fill fields (roadmap
19.1): stop price, time in force, outside hours, execution id and fee
currency, when set."""

from __future__ import annotations

from datetime import UTC, datetime

from stonks.core.types import Fill, Order
from stonks.production.tick import _record_fill, _record_order


def test_live_order_fields_are_written(state):
    order = Order(
        client_id="c1",
        ticker="AAPL.US",
        side="sell",
        quantity=5.0,
        order_type="stop_limit",
        limit_price=90.0,
        stop_price=91.0,
        time_in_force="gtc",
    )
    _record_order(state, order, "pending", portfolio_id="pf_default")
    row = state.sql("SELECT * FROM orders WHERE client_id = 'c1'")[0]
    assert (row["stop_price"], row["time_in_force"], row["outside_rth"]) == (91.0, "gtc", 0)


def test_a_plain_order_leaves_the_live_columns_at_their_defaults(state):
    _record_order(
        state, Order(client_id="c2", ticker="AAPL.US", side="buy", quantity=1.0), "pending"
    )
    row = state.sql("SELECT * FROM orders WHERE client_id = 'c2'")[0]
    assert row["stop_price"] is None and row["time_in_force"] is None
    assert row["outside_rth"] == 0 and row["broker_ref"] is None


def test_fill_execution_id_and_fee_currency_are_written(state):
    _record_order(
        state, Order(client_id="c3", ticker="AAPL.US", side="buy", quantity=1.0), "filled"
    )
    fill = Fill(
        order_client_id="c3",
        ticker="AAPL.US",
        quantity=1.0,
        price=10.0,
        fee=0.35,
        filled_at=datetime(2026, 9, 28, tzinfo=UTC),
        side="buy",
        broker_exec_id="e-1",
        fee_currency="USD",
    )
    _record_fill(state, fill, portfolio_id="pf_default")
    row = state.sql("SELECT broker_exec_id, fee_currency FROM fills")[0]
    assert (row["broker_exec_id"], row["fee_currency"]) == ("e-1", "USD")
