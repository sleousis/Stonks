"""execution.reconcile against a real SqliteState (tmp file) and fake brokers."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Fill, Order, Portfolio
from stonks.execution.brokers import AlpacaBroker
from stonks.execution.brokers.base import BrokerOrderState, OrderStateSource
from stonks.execution.reconcile import ReconcileSummary, reconcile_orders
from stonks.store.state import SqliteState

T0 = datetime(2026, 1, 5, 15, 30, tzinfo=UTC)


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


def insert_order(state, client_id, *, status="pending", ticker="AAPL.US", side="buy", qty=10.0):
    state.execute(
        "INSERT INTO orders (client_id, tick_id, strategy_id, ticker, side, quantity, order_type,"
        " limit_price, status, broker_order_id, created_at, updated_at)"
        " VALUES (?, NULL, NULL, ?, ?, ?, 'market', NULL, ?, NULL, 'x', 'x')",
        [client_id, ticker, side, qty, status],
    )


def order_row(state, client_id):
    return state.sql("SELECT * FROM orders WHERE client_id = ?", [client_id])[0]


def fills_for(state, client_id):
    return state.sql(
        "SELECT quantity, price, fee, filled_at FROM fills WHERE order_client_id = ? ORDER BY id",
        [client_id],
    )


class FakeStateBroker:
    """Broker with the OrderStateSource capability and a mutable order book."""

    def __init__(self) -> None:
        self.book: dict[str, BrokerOrderState] = {}
        self.errors: set[str] = set()
        self.queried: list[str] = []

    def set(self, client_id, status, filled, avg, *, qty=10.0, broker_id="b-1", side="buy"):
        self.book[client_id] = BrokerOrderState(
            client_id=client_id,
            broker_order_id=broker_id,
            ticker="AAPL.US",
            side=side,
            status=status,
            quantity=qty,
            filled_quantity=filled,
            avg_fill_price=avg,
            updated_at=T0,
        )

    def get_order_state(self, client_id):
        self.queried.append(client_id)
        if client_id in self.errors:
            raise RuntimeError("broker down")
        return self.book.get(client_id)

    def fetch_portfolio(self):
        return Portfolio(cash=0.0)

    def place_order(self, order):
        return None

    def reconcile(self):
        raise AssertionError("state-capable brokers are reconciled per order, not via fills")


def test_capability_detection():
    assert isinstance(AlpacaBroker(object()), OrderStateSource)
    assert not isinstance(SimulatedBroker(Portfolio(cash=0.0)), OrderStateSource)


# ---- order-state path (Alpaca-like) ------------------------------------------


def test_partial_then_full_fill_inserts_deltas_idempotently(state):
    broker = FakeStateBroker()
    insert_order(state, "c1")

    broker.set("c1", "partially_filled", 4.0, 100.0)
    s1 = reconcile_orders(broker, state)
    assert isinstance(s1, ReconcileSummary)
    assert s1.fills_inserted == 1
    assert s1.orders_updated == 1
    row = order_row(state, "c1")
    assert row["status"] == "partially_filled"
    assert row["broker_order_id"] == "b-1"

    broker.set("c1", "filled", 10.0, 101.2)
    s2 = reconcile_orders(broker, state)
    assert s2.fills_inserted == 1
    fills = fills_for(state, "c1")
    assert [f["quantity"] for f in fills] == pytest.approx([4.0, 6.0])
    assert fills[1]["price"] == pytest.approx(102.0)
    assert order_row(state, "c1")["status"] == "filled"

    # Terminal orders are no longer polled, and nothing is inserted twice.
    broker.queried.clear()
    s3 = reconcile_orders(broker, state)
    assert s3.fills_inserted == 0
    assert s3.orders_checked == 0
    assert broker.queried == []
    assert len(fills_for(state, "c1")) == 2


def test_rerun_with_unchanged_broker_state_is_a_noop(state):
    broker = FakeStateBroker()
    insert_order(state, "c1")
    broker.set("c1", "partially_filled", 4.0, 100.0)
    reconcile_orders(broker, state)
    s = reconcile_orders(broker, state)
    assert s.fills_inserted == 0
    assert s.orders_updated == 0
    assert len(fills_for(state, "c1")) == 1


def test_cancelled_after_partial_fill_books_fill_and_status(state):
    broker = FakeStateBroker()
    insert_order(state, "c1")
    broker.set("c1", "cancelled", 3.0, 50.0)
    s = reconcile_orders(broker, state)
    assert s.fills_inserted == 1
    assert order_row(state, "c1")["status"] == "cancelled"
    assert fills_for(state, "c1")[0]["quantity"] == pytest.approx(3.0)


def test_unknown_and_failing_orders_are_reported_and_left_alone(state):
    broker = FakeStateBroker()
    insert_order(state, "missing")
    insert_order(state, "boom")
    insert_order(state, "ok")
    broker.errors.add("boom")
    broker.set("ok", "filled", 10.0, 10.0)

    s = reconcile_orders(broker, state)
    assert s.unknown_orders == ("missing",)
    assert s.failed_orders == ("boom",)
    assert s.fills_inserted == 1
    assert order_row(state, "missing")["status"] == "pending"
    assert order_row(state, "boom")["status"] == "pending"
    assert order_row(state, "ok")["status"] == "filled"


def test_terminal_orders_are_not_queried(state):
    broker = FakeStateBroker()
    insert_order(state, "done", status="filled")
    insert_order(state, "rej", status="rejected")
    insert_order(state, "cxl", status="cancelled")
    reconcile_orders(broker, state)
    assert broker.queried == []


# ---- fill-list path (SimulatedBroker) -----------------------------------------


def simulated_with_fill(client_id="c1"):
    portfolio = Portfolio(cash=10_000.0)
    broker = SimulatedBroker(portfolio)
    broker.set_prices({"AAPL.US": 100.0}, as_of=date(2026, 1, 5))
    fill = broker.place_order(Order(client_id=client_id, ticker="AAPL.US", side="buy", quantity=5))
    assert fill is not None
    return broker, fill


def test_simulated_broker_fills_are_inserted_once(state):
    broker, _ = simulated_with_fill()
    insert_order(state, "c1", qty=5.0)

    s1 = reconcile_orders(broker, state)
    assert s1.fills_inserted == 1
    assert order_row(state, "c1")["status"] == "filled"

    s2 = reconcile_orders(broker, state)
    assert s2.fills_inserted == 0
    assert len(fills_for(state, "c1")) == 1


def test_simulated_fill_already_recorded_by_tick_is_not_duplicated(state):
    broker, fill = simulated_with_fill()
    insert_order(state, "c1", status="filled", qty=5.0)
    state.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            fill.order_client_id,
            fill.ticker,
            fill.quantity,
            fill.price,
            fill.fee,
            fill.filled_at.isoformat(timespec="seconds"),
        ],
    )
    s = reconcile_orders(broker, state)
    assert s.fills_inserted == 0
    assert len(fills_for(state, "c1")) == 1


def test_fill_without_order_row_is_skipped_not_crashing(state):
    broker, _ = simulated_with_fill("ghost")
    s = reconcile_orders(broker, state)
    assert s.fills_inserted == 0
    assert s.orphan_fills == ("ghost",)


def test_generic_broker_fill_list_is_deduplicated(state):
    class ListBroker:
        def __init__(self, fills):
            self.fills = fills

        def fetch_portfolio(self):
            return Portfolio(cash=0.0)

        def place_order(self, order):
            return None

        def reconcile(self):
            return list(self.fills)

    f = Fill("c1", "AAPL.US", 2.0, 10.0, 0.0, T0, "buy")
    insert_order(state, "c1", qty=2.0)
    broker = ListBroker([f, f])
    assert reconcile_orders(broker, state).fills_inserted == 1
    assert reconcile_orders(broker, state).fills_inserted == 0
