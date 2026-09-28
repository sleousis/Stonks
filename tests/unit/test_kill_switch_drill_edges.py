"""The kill switch drill at its edges (roadmap 19.11): the simulated
working broker's book, a step that raises, a broker with no global
cancel, and a cleanup that has to finish what a failed step left."""

from __future__ import annotations

import pytest

from stonks.core.types import Order
from stonks.production import drills
from stonks.production.drills import SimulatedWorkingBroker
from stonks.production.halts import list_halts
from tests.unit import test_kill_switch_drill as base

drill = base.drill
state = base.state  # the scratch state fixture


def order(cid="o1", side="buy", order_type="limit", limit=50.0) -> Order:
    return Order(client_id=cid, ticker="A.US", side=side, quantity=2.0, order_type=order_type,
                 limit_price=limit if order_type == "limit" else None)  # fmt: skip


# ---- the simulated working broker -------------------------------------------------------


def test_a_second_place_of_the_same_id_changes_nothing():
    broker = SimulatedWorkingBroker()
    assert broker.place_order(order()) is None
    assert broker.place_order(order(limit=500.0)) is None
    assert broker.working() == ["o1"] and broker.orders["o1"].limit_price == 50.0


def test_market_orders_fill_and_limits_fill_when_the_price_crosses():
    broker = SimulatedWorkingBroker(prices={"A.US": 100.0})
    fill = broker.place_order(order("m", order_type="market"))
    assert fill is not None and (fill.price, fill.quantity) == (100.0, 2.0)
    assert broker.place_order(order("s_low", side="sell", limit=90.0)) is not None
    assert broker.place_order(order("s_high", side="sell", limit=110.0)) is None
    assert broker.working() == ["s_high"]


def test_an_unknown_or_finished_order_is_not_cancelled():
    broker = SimulatedWorkingBroker(prices={"A.US": 100.0})
    assert broker.get_order_state("nope") is None
    assert not broker.cancel_order("nope")
    broker.place_order(order("m", order_type="market"))
    assert not broker.cancel_order("m")
    assert broker.get_order_state("m").status == "filled"  # type: ignore[union-attr]


# ---- the drill ---------------------------------------------------------------------------


def test_a_reference_price_must_be_positive(state):
    with pytest.raises(ValueError, match="reference_price must be positive"):
        drill(state, SimulatedWorkingBroker(), reference_price=0.0)


class NoGlobalCancel:
    """The simulated broker without ``cancel_all``."""

    broker_kind = "simulated"

    def __init__(self) -> None:
        self._inner = SimulatedWorkingBroker()
        self.orders = self._inner.orders

    def place_order(self, order):
        return self._inner.place_order(order)

    def get_order_state(self, client_id):
        return self._inner.get_order_state(client_id)

    def cancel_order(self, client_id):
        return self._inner.cancel_order(client_id)


def test_a_broker_without_a_global_cancel_skips_that_step(state):
    report = drill(state, NoGlobalCancel())
    assert report.passed, report.steps
    step = report.step("global_cancel")
    assert step is not None and step.ok and "skipped" in step.detail


class StuckCancel(SimulatedWorkingBroker):
    def cancel_order(self, client_id: str) -> bool:
        raise ConnectionError("gateway gone")


def test_a_step_that_raises_fails_the_drill_and_the_cleanup_never_raises(state):
    def engage(_state):
        raise RuntimeError("halt table locked")

    broker = StuckCancel()
    report = drill(state, broker, engage=engage)
    assert not report.passed and report.halt_id is None
    assert [s.name for s in report.steps] == ["place_working_order", "engage_kill"]
    assert report.steps[1].detail == "RuntimeError: halt table locked"
    assert len(broker.working()) == 1  # the cleanup tried, the broker refused


def test_the_cleanup_clears_a_halt_the_resume_could_not(state, monkeypatch):
    real = drills.clear_halt
    calls = []

    def flaky(*args, **kwargs):
        calls.append(kwargs["reason"])
        if len(calls) == 1:
            raise RuntimeError("busy")
        return real(*args, **kwargs)

    monkeypatch.setattr(drills, "clear_halt", flaky)
    report = drill(state, SimulatedWorkingBroker())
    assert not report.passed and not report.step("resume").ok  # type: ignore[union-attr]
    assert calls == ["drill finished", "drill cleanup"]
    assert list_halts(state) == []


def test_a_cleanup_that_cannot_clear_the_halt_is_logged(state, monkeypatch):
    def never(*args, **kwargs):
        raise RuntimeError("busy")

    monkeypatch.setattr(drills, "clear_halt", never)
    report = drill(state, SimulatedWorkingBroker())
    assert not report.passed
    assert [h.id for h in list_halts(state)] == [report.halt_id]
