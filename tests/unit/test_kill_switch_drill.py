"""The kill switch drill (roadmap 19.11, ``production.drills``)."""

from __future__ import annotations

import pytest

from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.execution.cancel import CANCEL_REASON
from stonks.production.drills import (
    DrillRefusedError,
    SimulatedWorkingBroker,
    run_kill_switch_drill,
)
from stonks.production.halts import list_halts, trip_halt
from stonks.store.state import SqliteState
from tests.fakes.ib_gateway import FakeIbGateway

STEPS = [
    "place_working_order",
    "engage_kill",
    "new_orders_blocked",
    "working_order_cancelled",
    "global_cancel",
    "resume",
]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "scratch.sqlite")
    s.migrate()
    yield s
    s.close()


def drill(state, broker, **kw):
    clock = FakeClock()
    kw.setdefault("clock", clock)
    kw.setdefault("sleep", clock.sleep)
    return run_kill_switch_drill(state, broker, **kw)


def test_simulated_drill_passes_and_leaves_nothing_working(state):
    broker = SimulatedWorkingBroker()
    report = drill(state, broker)
    assert report.passed, report.steps
    assert [s.name for s in report.steps] == STEPS
    assert report.broker == "simulated"
    assert broker.working() == []
    row = state.sql("SELECT status, status_reason FROM orders WHERE client_id = ?",
                    [report.client_id])[0]  # fmt: skip
    assert (row["status"], row["status_reason"]) == ("cancelled", CANCEL_REASON)
    assert list_halts(state) == []  # the scratch halt is cleared at the end
    assert report.cancel_seconds is not None and report.cancel_seconds <= 10


def test_the_drill_order_is_a_tiny_limit_far_below_the_market(state):
    broker = SimulatedWorkingBroker(prices={"AAPL.US": 200.0})
    report = drill(state, broker, reference_price=200.0)
    (order,) = broker.orders.values()
    assert order.quantity == 1 and order.side == "buy" and order.order_type == "limit"
    assert order.limit_price is not None and order.limit_price <= 100.0
    assert report.passed


def test_drill_through_the_ibkr_adapter_on_the_fake_gateway(state):
    gw = FakeIbGateway()
    broker = IbkrBroker(gw, mode="paper")
    report = drill(state, broker, reference_price=200.0)
    assert report.passed, report.steps
    assert report.broker == "ibkr"
    assert gw.global_cancels == 1
    assert gw.open_trades() == []


def test_a_cancel_that_stays_pending_fails_the_drill(state):
    gw = FakeIbGateway()
    gw.cancel_leaves_pending = True
    broker = IbkrBroker(gw, mode="paper")
    gw.global_cancel = lambda: None  # type: ignore[method-assign]
    report = drill(state, broker, reference_price=200.0, cancel_timeout=2.0)
    assert not report.passed
    step = report.step("working_order_cancelled")
    assert step is not None and not step.ok
    assert list_halts(state) == []  # cleaned up whatever happened


def test_an_order_that_fills_fails_before_the_kill(state):
    broker = SimulatedWorkingBroker(prices={"AAPL.US": 10.0})  # the limit crosses
    report = drill(state, broker, reference_price=100.0)
    assert not report.passed
    assert report.steps[0].name == "place_working_order" and not report.steps[0].ok
    assert len(report.steps) == 1
    assert list_halts(state) == []


def test_an_engage_that_does_not_cancel_is_caught(state):
    def halt_only(s: SqliteState) -> int:
        halt, _ = trip_halt(s, "kill", reason="drill", actor="test", scope="global", halt="all")
        return halt.id

    broker = SimulatedWorkingBroker()
    report = drill(state, broker, engage=halt_only, cancel_timeout=1.0)
    step = report.step("working_order_cancelled")
    assert step is not None and not step.ok
    assert not report.passed
    assert broker.working() == []  # the cleanup cancelled it


def test_a_live_broker_is_refused(state):
    live = IbkrBroker(FakeIbGateway(["U7654321"]), mode="live", allow_live=True)
    with pytest.raises(DrillRefusedError):
        drill(state, live)
    assert state.sql("SELECT COUNT(*) AS n FROM orders")[0]["n"] == 0


def test_a_live_mode_attribute_is_refused(state):
    broker = SimulatedWorkingBroker()
    broker.mode = "live"  # type: ignore[attr-defined]
    with pytest.raises(DrillRefusedError):
        drill(state, broker)


def test_a_broker_without_cancel_is_refused(state):
    class NoCancel:
        def place_order(self, order):
            return None

    with pytest.raises(DrillRefusedError):
        drill(state, NoCancel())


def test_an_open_halt_refuses_the_drill(state):
    trip_halt(state, "kill", reason="real", actor="ops", scope="global", halt="all")
    with pytest.raises(DrillRefusedError):
        drill(state, SimulatedWorkingBroker())


def test_report_to_dict(state):
    data = drill(state, SimulatedWorkingBroker()).to_dict()
    assert data["passed"] is True
    assert [s["name"] for s in data["steps"]] == STEPS
