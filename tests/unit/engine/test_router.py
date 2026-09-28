"""The intraday router: orders through the state machine, fills per event
(roadmap 21.2.3). Hermetic: the simulated intraday broker and
``FakeIbGateway`` behind ``IbkrBroker``."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Self

import pytest

from stonks.accounts.models import DEFAULT_PORTFOLIO_ID
from stonks.backtest.costs import FixedCostModel
from stonks.core.clock import FakeClock
from stonks.core.interval import Interval
from stonks.core.stream import StreamBar, StreamEvent
from stonks.core.types import Fill, Order, Portfolio
from stonks.engine.driver import BarClose, EventDriver
from stonks.engine.router import (
    ROUTER_PRIORITY,
    IntradayRouter,
    OrderRouter,
    RouteAck,
    as_day_order,
)
from stonks.engine.sim_broker import IntradaySimBroker
from stonks.execution.brokers.base import OrderRejectedError
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.execution.order_state import ReconciliationPendingError, current_state, write_state
from stonks.store.state import SqliteState
from stonks.streaming.base import StreamContext, StreamingSource
from tests.fakes.ib_gateway import FakeIbGateway

M0 = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
ONE = timedelta(minutes=1)
SETTLE = timedelta(seconds=2)
PF = DEFAULT_PORTFOLIO_ID


def sbar(minute: int, ticker: str = "A.US", open_: float = 10.0, volume: int = 10_000) -> StreamBar:
    ts = M0 + minute * ONE
    return StreamBar(ticker, ts, Interval.MIN_1, open_, open_ + 0.5, open_ - 0.5, open_, volume)


def close_of(seq: int, *bars: StreamBar) -> BarClose:
    return BarClose(at=bars[0].timestamp + ONE, interval=Interval.MIN_1, bars=bars, sequence=seq)


def buy(cid: str = "c1", ticker: str = "A.US", qty: float = 100.0, **kw) -> Order:
    kw.setdefault("decision_price", 10.0)
    return Order(client_id=cid, ticker=ticker, side="buy", quantity=qty, **kw)


def same_day(ticker: str, at: datetime) -> date:
    return at.date()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(M0 + ONE + SETTLE)


def sim_router(
    state: SqliteState, clock: FakeClock, **kw
) -> tuple[IntradayRouter, IntradaySimBroker]:
    broker = IntradaySimBroker(
        Portfolio(cash=1_000_000.0, positions={}), clock=clock, session_key=same_day, **kw
    )
    router = IntradayRouter(broker, state, portfolio_id=PF, clock=clock)
    router.start()
    return router, broker


def fills(state: SqliteState, cid: str) -> list[dict]:
    return [
        dict(r)
        for r in state.sql(
            "SELECT quantity, price, fee, filled_at, broker_exec_id FROM fills"
            " WHERE order_client_id = ? ORDER BY filled_at",
            [cid],
        )
    ]


def order_row(state: SqliteState, cid: str) -> dict:
    return dict(state.sql("SELECT * FROM orders WHERE client_id = ?", [cid])[0])


# ---- the interface ----------------------------------------------------------------------------


def test_router_satisfies_the_interface_the_step_calls(state, clock):
    router, _ = sim_router(state, clock)
    assert isinstance(router, OrderRouter)


def test_ack_says_whether_the_order_went_out():
    assert RouteAck("c", "A.US", "sent", "submitted").accepted
    assert RouteAck("c", "A.US", "known", "accepted").accepted
    for status in ("rejected", "unknown", "held"):
        assert not RouteAck("c", "A.US", status, None).accepted  # type: ignore[arg-type]


# ---- day orders -------------------------------------------------------------------------------


def test_as_day_order_sets_day_when_none():
    assert as_day_order(buy()).time_in_force == "day"
    assert as_day_order(buy(time_in_force="ioc")).time_in_force == "ioc"


@pytest.mark.parametrize("tif", ["opg", "gtc"])
def test_as_day_order_refuses_orders_that_outlive_the_session(tif):
    kw = {"order_type": "stop", "stop_price": 9.0} if tif == "gtc" else {}
    with pytest.raises(OrderRejectedError, match="intraday"):
        as_day_order(buy(time_in_force=tif, **kw))


# ---- the simulated broker ---------------------------------------------------------------------


def test_route_writes_the_order_submitted_and_fills_on_the_next_bar(state, clock):
    router, _ = sim_router(state, clock)
    (ack,) = router.route([buy()])
    assert (ack.client_id, ack.status, ack.state) == ("c1", "sent", "accepted")
    row = order_row(state, "c1")
    assert (row["state"], row["status"], row["time_in_force"]) == ("accepted", "pending", "day")
    assert row["broker_order_id"] == "sim-1"
    assert row["portfolio_id"] == PF
    assert fills(state, "c1") == []

    clock.set(M0 + 2 * ONE + SETTLE)
    summary = router.on_bar_close(close_of(1, sbar(1, open_=10.2)))
    assert summary.fills_inserted == 1
    (f,) = fills(state, "c1")
    assert (f["quantity"], f["price"]) == (100.0, pytest.approx(10.2))
    assert f["filled_at"].startswith("2026-09-28T13:31:00")
    assert f["broker_exec_id"] == "sim-1.1"
    assert current_state(state, "c1") == "filled"


def test_fee_of_the_cost_model_reaches_the_ledger(state, clock):
    router, _ = sim_router(state, clock, cost_model=FixedCostModel(fee_per_trade=1.25))
    router.route([buy()])
    router.on_bar_close(close_of(1, sbar(1)))
    (f,) = fills(state, "c1")
    assert f["fee"] == pytest.approx(1.25)


def test_participation_cap_books_a_partial_fill_per_bar(state, clock):
    router, _ = sim_router(state, clock)
    router.route([buy(qty=1_500.0)])
    router.on_bar_close(close_of(1, sbar(1, open_=10.0)))
    assert current_state(state, "c1") == "partially_filled"
    router.on_bar_close(close_of(2, sbar(2, open_=11.0)))
    assert current_state(state, "c1") == "filled"
    assert [f["quantity"] for f in fills(state, "c1")] == [1_000.0, 500.0]


def test_rest_of_a_day_order_expires_in_the_next_session(state, clock):
    router, _ = sim_router(state, clock)
    router.route([buy(qty=1_500.0)])
    router.on_bar_close(close_of(1, sbar(1)))
    tomorrow = StreamBar("A.US", M0 + timedelta(days=1), Interval.MIN_1, 10, 10.5, 9.5, 10, 10_000)
    router.on_bar_close(close_of(2, tomorrow))
    assert current_state(state, "c1") == "expired"
    assert order_row(state, "c1")["status"] == "cancelled"


def test_an_order_waiting_for_its_bar_is_accepted(state, clock):
    router, _ = sim_router(state, clock)
    router.route([buy()])
    router.on_bar_close(close_of(1, sbar(1, ticker="B.US")))
    assert current_state(state, "c1") == "accepted"


def test_routing_the_same_client_id_again_is_known_and_sends_nothing(state, clock):
    router, broker = sim_router(state, clock)
    router.route([buy()])
    (ack,) = router.route([buy(qty=500.0)])
    assert (ack.status, ack.state) == ("known", "accepted")
    router.on_bar_close(close_of(1, sbar(1)))
    assert [f["quantity"] for f in fills(state, "c1")] == [100.0]


def test_decision_time_defaults_to_the_clock(state, clock):
    router, _ = sim_router(state, clock)
    router.route([buy()])
    assert order_row(state, "c1")["decided_at"].startswith("2026-09-28T13:31:02")


class RejectingBroker(IntradaySimBroker):
    def place_order(self, order: Order) -> Fill | None:
        raise OrderRejectedError("no such ticker")


class TimeoutBroker(IntradaySimBroker):
    """The first A.US submit reaches the broker but its answer is lost."""

    lost = False

    def place_order(self, order: Order) -> Fill | None:
        if order.ticker == "A.US" and not self.lost:
            self.lost = True
            super().place_order(order)
            raise TimeoutError("no answer")
        return super().place_order(order)


def test_broker_rejection_ends_the_order_rejected(state, clock):
    broker = RejectingBroker(Portfolio(cash=1e6, positions={}), clock=clock, session_key=same_day)
    router = IntradayRouter(broker, state, portfolio_id=PF, clock=clock)
    router.start()
    (ack,) = router.route([buy()])
    assert (ack.status, ack.state, ack.reason) == ("rejected", "rejected", "no such ticker")
    assert current_state(state, "c1") == "rejected"


def test_an_order_that_outlives_the_session_is_rejected_before_the_broker(state, clock):
    router, broker = sim_router(state, clock)
    (ack,) = router.route([buy(time_in_force="opg")])
    assert ack.status == "rejected"
    assert "intraday" in (ack.reason or "")
    assert broker.get_order_state("c1") is None


def test_no_answer_marks_unknown_and_holds_that_ticker_only(state, clock):
    broker = TimeoutBroker(Portfolio(cash=1e6, positions={}), clock=clock, session_key=same_day)
    router = IntradayRouter(broker, state, portfolio_id=PF, clock=clock)
    router.start()
    first, other = router.route([buy(), buy(cid="c2", ticker="B.US")])
    assert (first.status, first.state) == ("unknown", "unknown")
    assert other.status == "sent"
    (held,) = router.route([buy(cid="c3")])
    assert (held.status, held.state) == ("held", None)
    assert current_state(state, "c3") is None
    # reconciliation on the next event finds the order at the broker
    router.on_bar_close(close_of(1, sbar(1), sbar(1, ticker="B.US")))
    assert current_state(state, "c1") == "filled"
    (again,) = router.route([buy(cid="c4")])
    assert again.status == "sent"


def test_nothing_is_sent_before_start(state, clock):
    broker = IntradaySimBroker(Portfolio(cash=1e6, positions={}), clock=clock, session_key=same_day)
    router = IntradayRouter(broker, state, portfolio_id=PF, clock=clock)
    (ack,) = router.route([buy()])
    assert ack.status == "held"
    assert "start" in (ack.reason or "")
    assert not router.ready


def test_start_refuses_while_an_order_stays_unknown(state, clock):
    broker = IntradaySimBroker(Portfolio(cash=1e6, positions={}), clock=clock, session_key=same_day)
    router = IntradayRouter(broker, state, portfolio_id=PF, clock=clock)
    router.start()
    router.route([buy()])
    write_state(state, "c1", "unknown", reason="lost")
    restarted = IntradayRouter(
        IntradaySimBroker(Portfolio(cash=1e6, positions={}), clock=clock, session_key=same_day),
        state,
        portfolio_id=PF,
        clock=clock,
    )
    with pytest.raises(ReconciliationPendingError):
        restarted.start()
    assert not restarted.ready


# ---- IBKR -------------------------------------------------------------------------------------


def ib_router(state: SqliteState, clock: FakeClock, gw: FakeIbGateway | None = None):
    gw = gw or FakeIbGateway(now=M0)
    broker = IbkrBroker(gw, mode="paper", intraday=True, clock=clock)
    router = IntradayRouter(broker, state, portfolio_id=PF, clock=clock)
    router.start()
    return router, gw


def ib_buy(cid: str = "c1", **kw) -> Order:
    return buy(cid=cid, ticker="AAPL.US", qty=10.0, decision_price=200.0, **kw)


def test_ibkr_route_sends_a_day_order(state, clock):
    router, gw = ib_router(state, clock)
    (ack,) = router.route([ib_buy()])
    assert (ack.status, ack.state) == ("sent", "accepted")
    _, req = gw.sent[0]
    assert (req.tif, req.order_type, req.outside_rth) == ("DAY", "LMT", False)


def test_ibkr_fills_are_reconciled_on_each_event(state, clock):
    router, gw = ib_router(state, clock)
    router.route([ib_buy()])
    gw.fill("c1", 4, 200.1, commission=0.35, at=M0 + ONE + timedelta(seconds=1))
    router.on_bar_close(close_of(1, sbar(1, ticker="AAPL.US")))
    assert current_state(state, "c1") == "partially_filled"
    gw.fill("c1", 6, 200.2, commission=0.4, at=M0 + 2 * ONE)
    router.on_bar_close(close_of(2, sbar(2, ticker="AAPL.US")))
    assert current_state(state, "c1") == "filled"
    booked = fills(state, "c1")
    assert [(f["quantity"], f["fee"]) for f in booked] == [(4.0, 0.35), (6.0, 0.4)]
    # a repeat of the same event books nothing twice
    assert router.on_bar_close(close_of(3, sbar(3, ticker="AAPL.US"))).fills_inserted == 0


def test_ibkr_day_order_expiring_at_the_close_is_reconciled(state, clock):
    router, gw = ib_router(state, clock)
    router.route([ib_buy()])
    gw.set_status("c1", "Cancelled", "day order expired")
    router.on_bar_close(close_of(1, sbar(1, ticker="AAPL.US")))
    assert current_state(state, "c1") in ("cancelled", "expired")


def test_ibkr_timeout_is_unknown_then_resolved_by_client_id(state, clock):
    router, gw = ib_router(state, clock)
    gw.submit_fault = "timeout_after"
    (ack,) = router.route([ib_buy()])
    assert ack.status == "unknown"
    assert current_state(state, "c1") == "unknown"
    assert router.route([ib_buy(cid="c2")])[0].status == "held"
    router.on_bar_close(close_of(1, sbar(1, ticker="AAPL.US")))
    assert current_state(state, "c1") == "accepted"
    assert gw.sent_count("c1") == 1
    assert router.route([ib_buy(cid="c2")])[0].status == "sent"


# ---- on the event driver ----------------------------------------------------------------------


class ListSource(StreamingSource):
    finite = True

    def __init__(self, events: Sequence[StreamEvent]) -> None:
        self.events = list(events)

    @classmethod
    def from_settings(cls, ctx: StreamContext) -> Self:
        raise NotImplementedError

    def stream(self, tickers: Sequence[str]) -> Iterator[StreamEvent]:
        yield from self.events

    def close(self) -> None:
        pass


def test_on_the_driver_a_decision_on_minute_zero_fills_at_the_open_of_minute_one(state):
    clock = FakeClock(M0)
    broker = IntradaySimBroker(Portfolio(cash=1e6, positions={}), clock=clock, session_key=same_day)
    router = IntradayRouter(broker, state, portfolio_id=PF, clock=clock)
    router.start()
    driver = EventDriver(ListSource([sbar(m, open_=10.0 + m) for m in range(3)]), clock=clock)
    acks: list[RouteAck] = []

    def step(event: BarClose) -> None:
        if event.sequence == 1:  # the close of minute 0
            acks.extend(router.route([buy(decided_at=clock.now())]))

    driver.register(router, name="router", priority=ROUTER_PRIORITY)
    driver.register(step, name="step")
    assert driver.handler_names() == ["router", "step"]
    driver.run(["A.US"])
    assert acks[0].status == "sent"
    (f,) = fills(state, "c1")
    assert f["price"] == pytest.approx(11.0)  # the open of minute 1
    assert f["filled_at"].startswith("2026-09-28T13:31:00")
    assert driver.stats.total_handler_errors == 0


def test_hold_working_keeps_one_working_order_per_ticker(state, clock) -> None:
    broker = IntradaySimBroker(
        Portfolio(cash=1_000_000.0, positions={}), clock=clock, session_key=same_day
    )
    router = IntradayRouter(broker, state, portfolio_id=PF, clock=clock, hold_working=True)
    router.start()
    assert [a.status for a in router.route([buy("c1")])] == ["sent"]
    # the next bar decides the same trade again: held while c1 works
    (ack,) = router.route([buy("c2")])
    assert (ack.status, ack.state) == ("held", None)
    assert "working" in (ack.reason or "")
    # both legs of one batch still go out, and another ticker is free
    acks = router.route([buy("c3", ticker="B.US"), buy("c4", ticker="B.US")])
    assert [a.status for a in acks] == ["sent", "sent"]
    # once c1 settles the ticker is free again
    router.on_bar_close(close_of(1, sbar(1)))
    assert current_state(state, "c1") == "filled"
    assert [a.status for a in router.route([buy("c5")])] == ["sent"]
