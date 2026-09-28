"""IBKR native execution algos (roadmap 23.16): Adaptive, VWAP and TWAP go out
as one order with ``algoStrategy``, against ``FakeIbGateway``."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from stonks.core.instruments import InstrumentSpec
from stonks.core.types import Order
from stonks.execution.algos import algo_spec, with_window
from stonks.execution.brokers.base import NativeAlgoBroker, OrderRejectedError
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.execution.brokers.ibkr.client import IbOrderRequest
from stonks.execution.brokers.ibkr.orders import to_ib_order
from stonks.execution.brokers.ibkr.settings import IbkrOrderSettings
from stonks.execution.reconcile import reconcile_orders
from tests.fakes.ib_gateway import FakeIbGateway, _algo_rejection

SPEC = InstrumentSpec.spot("AAPL.US", tick_size=0.01)
OPEN = datetime(2026, 9, 29, 13, 30, tzinfo=UTC)
CLOSE = datetime(2026, 9, 29, 20, 0, tzinfo=UTC)


def order(**kw) -> Order:
    base = {"client_id": "t1-s1-AAPL.US-buy", "ticker": "AAPL.US", "side": "buy",
            "quantity": 10.0, "decision_price": 200.0}  # fmt: skip
    base.update(kw)
    return Order(**base)


def ib(o: Order) -> IbOrderRequest:
    return to_ib_order(o, SPEC, account="DU1", settings=IbkrOrderSettings())


def test_plain_order_carries_no_algo():
    req = ib(order())
    assert req.algo_strategy is None
    assert req.algo_params == ()


def test_adaptive_goes_out_as_a_collared_day_limit():
    req = ib(order(algo=algo_spec("adaptive", {"priority": "urgent"})))
    assert req.order_type == "LMT"
    assert req.tif == "DAY"
    assert req.algo_strategy == "Adaptive"
    assert req.algo_params == (("adaptivePriority", "Urgent"),)


def test_vwap_carries_its_window_in_utc():
    spec = with_window(algo_spec("vwap", {"end_minutes": 90}), OPEN, CLOSE)
    req = ib(order(order_type="limit", limit_price=201.0, algo=spec))
    assert req.algo_strategy == "Vwap"
    tags = dict(req.algo_params)
    assert tags["startTime"] == "20260929-13:30:00"
    assert tags["endTime"] == "20260929-15:00:00"
    assert req.limit_price == pytest.approx(201.0)


def test_algo_refuses_stops_and_non_day_orders():
    with pytest.raises(OrderRejectedError, match="stop"):
        ib(order(order_type="stop", stop_price=190.0, algo=algo_spec("adaptive")))
    with pytest.raises(OrderRejectedError, match="day order"):
        ib(order(time_in_force="opg", algo=algo_spec("twap")))


def test_unknown_algo_is_rejected_before_sending():
    with pytest.raises(OrderRejectedError, match="unknown execution algo"):
        ib(order(algo={"name": "iceberg", "params": {}}))


def test_ibkr_broker_runs_the_three_algos_natively():
    broker = IbkrBroker(FakeIbGateway(), mode="paper")
    assert isinstance(broker, NativeAlgoBroker)
    assert broker.native_algos == frozenset({"adaptive", "twap", "vwap"})


def test_algo_parent_fills_through_ibkr_children_as_one_order(tmp_path):
    from stonks.store.state import SqliteState

    gw = FakeIbGateway()
    broker = IbkrBroker(gw, mode="paper")
    spec = with_window(algo_spec("twap", {"end_minutes": 60}), OPEN, CLOSE)
    parent = order(algo=spec)
    state = SqliteState(tmp_path / "s.sqlite")
    state.migrate()
    from stonks.production.tick import _record_order

    _record_order(state, parent, status="pending", portfolio_id="pf_default")
    broker.place_order(parent)
    ref = broker.broker_ref(parent.client_id)
    assert gw.algo_orders[ref][0] == "Twap"
    gw.work_algo(ref, [(4, 200.1), (3, 200.2), (3, 200.3)], commission=0.35)
    summary = reconcile_orders(broker, state, portfolio_id="pf_default")
    assert summary.fills_inserted == 3
    row = state.sql("SELECT status, exec_algo FROM orders WHERE client_id = ?", [parent.client_id])
    state.close()
    assert row[0]["status"] == "filled"
    assert row[0]["exec_algo"] == "twap"


def test_fake_gateway_refuses_what_ibkr_refuses():
    base = IbOrderRequest(
        action="BUY", total_quantity=1, order_type="LMT", tif="DAY", order_ref="r",
        account="DU1", limit_price=1.0,
    )  # fmt: skip
    from dataclasses import replace

    assert _algo_rejection(replace(base, algo_strategy="Adaptive",
                                   algo_params=(("adaptivePriority", "Normal"),))) is None  # fmt: skip
    assert _algo_rejection(replace(base, algo_strategy="Iceberg"))[1].startswith("Invalid")  # type: ignore[index]
    stp = replace(base, order_type="STP", algo_strategy="Twap")
    assert "does not take" in _algo_rejection(stp)[1]  # type: ignore[index]
    opg = replace(base, tif="OPG", algo_strategy="Twap")
    assert "DAY" in _algo_rejection(opg)[1]  # type: ignore[index]
    late = replace(
        base,
        algo_strategy="Vwap",
        algo_params=(("startTime", "20260929-15:00:00"), ("endTime", "20260929-14:00:00")),
    )
    assert "after" in _algo_rejection(late)[1]  # type: ignore[index]


def test_a_stored_window_goes_out_as_ibkr_times():
    gw = FakeIbGateway()
    broker = IbkrBroker(gw, mode="paper")
    ok = order(algo={"name": "vwap", "params": {"max_participation": 0.1},
                      "window": {"start": "2026-09-29T15:00:00+00:00",
                                 "end": "2026-09-29T16:00:00+00:00"}})  # fmt: skip
    broker.place_order(ok)
    [(strategy, tags)] = gw.algo_orders.values()
    assert strategy == "Vwap"
    assert tags["startTime"] == "20260929-15:00:00"
