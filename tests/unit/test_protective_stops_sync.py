"""Protective stops against the ledger and a broker (roadmap 19.10): the
failure paths of sending a plan, cancelling a stop at the broker, the
paper ledger and the ``live_stops`` job. The happy paths at IBKR live in
``tests/integration/test_protective_stops_ibkr.py``."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from stonks.core.types import Order, Portfolio
from stonks.execution.brokers.base import BrokerOrderState, OrderRejectedError
from stonks.execution.order_state import current_state, mark_unknown, write_state
from stonks.production.live import stops
from stonks.production.live.stops import (
    LedgerFill,
    LiveBook,
    StopCancel,
    StopPlan,
    StopSync,
    WorkingStop,
    holdings_from_fills,
    load_atr,
    load_working_stops,
    record_paper_plan,
    send_stop_plan,
    stop_ids,
    sweep_paper_stops,
    sync_live_books,
)
from stonks.production.rules._stop_settings import ProtectiveStopSettings

PF = "pf_default"
AS_OF = date(2026, 3, 17)
ON = ProtectiveStopSettings(enabled=True)


@pytest.fixture
def state(state):
    state.execute(
        "INSERT INTO strategies (id, class_path, params_json, artifact_path, status, created_at,"
        " updated_at) VALUES ('s1', 'x.Y', '{}', 'a', 'active', 'x', 'x')"
    )
    return state


def stop_order(cid="e1:stop", ticker="A.US", qty=10.0, price=90.0, decided=AS_OF) -> Order:
    return Order(
        client_id=cid,
        ticker=ticker,
        side="sell",
        quantity=qty,
        order_type="stop",
        stop_price=price,
        time_in_force="gtc",
        position_effect="close",
        strategy_id="s1",
        portfolio_id=PF,
        oca_group="stk-oca-x",
        decided_at=datetime.combine(decided, datetime.min.time(), UTC),
        decision_context={"trigger": "stop", "entry_client_id": "e1"},
    )


def working(cid="e1:stop", state="accepted") -> WorkingStop:
    return WorkingStop(client_id=cid, ticker="A.US", side="sell", quantity=10.0, stop_price=90.0,
                       strategy_id="s1", oca_group="stk-oca-x", entry_client_id="e1",
                       state=state)  # fmt: skip


def paper_stop(state, order: Order | None = None) -> Order:
    """A stop working in the ledger (``accepted``)."""
    order = order or stop_order()
    record_paper_plan(state, StopPlan(place=(order,)), portfolio_id=PF)
    return order


class PlainBroker:
    """Places orders; no cancel, no lookup by client id."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.placed: list[str] = []

    def place_order(self, order: Order):
        if self.error is not None:
            raise self.error
        self.placed.append(order.client_id)


class LookupBroker(PlainBroker):
    """Also looks orders up and cancels them."""

    def __init__(self, *, error=None, cancel=True, cancel_error=None, lookup_error=None,
                 known: BrokerOrderState | None = None) -> None:  # fmt: skip
        super().__init__(error)
        self.cancel = cancel
        self.cancel_error = cancel_error
        self.lookup_error = lookup_error
        self.known = known
        self.cancelled: list[str] = []

    def get_order_state(self, client_id: str) -> BrokerOrderState | None:
        if self.lookup_error is not None:
            raise self.lookup_error
        return self.known

    def cancel_order(self, client_id: str) -> bool:
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancelled.append(client_id)
        return self.cancel


class CancelOnly:
    """Cancels but cannot look orders up."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer

    def cancel_order(self, client_id: str) -> bool:
        return self.answer


# ---- the values ------------------------------------------------------------------


def test_a_sync_reports_failures_unprotected_positions_and_fills():
    sync = StopSync(placed=("a",), failed=("b",), unprotected=(("A.US", "no room"),),
                    filled=("c",))  # fmt: skip
    assert sync.as_dict() == {"placed": 1, "cancelled": 0, "kept": 0, "failed": ["b"],
                              "unprotected": ["A.US: no room"], "filled": ["c"]}  # fmt: skip
    assert StopSync().as_dict() == {"placed": 0, "cancelled": 0, "kept": 0}


def test_a_fill_that_crosses_zero_opens_the_other_side_at_its_price():
    fills = [
        LedgerFill("e1", "s1", "A.US", "buy", 10.0, 100.0),
        LedgerFill("x1", "s2", "A.US", "sell", 15.0, 110.0),
    ]
    [(ticker, h)] = holdings_from_fills(fills, {"A.US": -5.0}).items()
    assert (ticker, h.quantity, h.entry_client_id, h.strategy_id) == ("A.US", -5.0, "x1", "s2")
    assert h.entry_price == pytest.approx(110.0)


# ---- the ledger ------------------------------------------------------------------


def test_a_ledger_before_the_stop_columns_has_no_stops(state, monkeypatch):
    paper_stop(state)
    monkeypatch.setattr(stops, "stops_recorded", lambda _s: False)
    assert load_working_stops(state, PF) == []
    assert stop_ids(state, PF) == set()


@pytest.mark.parametrize("raw", [None, "not json", "[1, 2]"])
def test_a_stop_with_an_unreadable_context_protects_no_entry(state, raw):
    paper_stop(state)
    state.execute("UPDATE orders SET decision_context_json = ?", [raw])
    [stop] = load_working_stops(state, PF)
    assert stop.client_id == "e1:stop" and stop.entry_client_id is None


def test_too_few_bars_give_a_close_but_no_atr(lake):
    days = pd.date_range("2026-03-10", periods=3, freq="D")
    lake.upsert_prices(pd.DataFrame({
        "ticker": "A.US", "date": days, "open": 10.0, "high": [11.0, 12.0, 13.0],
        "low": 9.0, "close": [10.0, 11.0, 12.5], "adj_close": 10.0, "volume": 100.0,
    }))  # fmt: skip
    atr, closes = load_atr(lake, ["A.US"], AS_OF, window=14)
    assert atr == {} and closes == {"A.US": pytest.approx(12.5)}


# ---- sending a plan to a real broker --------------------------------------------------


def test_a_rejected_stop_is_rejected_and_reported_failed(state):
    order = stop_order()
    sync = send_stop_plan(state, PlainBroker(OrderRejectedError("no shares")),
                          StopPlan(place=(order,)), portfolio_id=PF)  # fmt: skip
    assert sync.failed == ("e1:stop",) and sync.placed == ()
    assert current_state(state, "e1:stop") == "rejected"


def test_a_stop_with_no_answer_is_unknown_until_reconciled(state):
    sync = send_stop_plan(state, PlainBroker(TimeoutError("gateway")),
                          StopPlan(place=(stop_order(),)), portfolio_id=PF)  # fmt: skip
    assert sync.failed == ("e1:stop",)
    assert current_state(state, "e1:stop") == "unknown"


def test_a_stop_sent_before_is_not_sent_again(state):
    paper_stop(state)
    broker = PlainBroker()
    sync = send_stop_plan(state, broker, StopPlan(place=(stop_order(),)), portfolio_id=PF)
    assert broker.placed == [] and sync.placed == ()


def test_a_broker_without_lookup_leaves_the_stop_submitted(state):
    broker = PlainBroker()
    sync = send_stop_plan(state, broker, StopPlan(place=(stop_order(),)), portfolio_id=PF)
    assert sync.placed == ("e1:stop",) and broker.placed == ["e1:stop"]
    assert current_state(state, "e1:stop") == "submitted"


def test_a_failed_sync_after_the_send_still_counts_as_placed(state):
    broker = LookupBroker(lookup_error=ConnectionError("dropped"))
    sync = send_stop_plan(state, broker, StopPlan(place=(stop_order(),)), portfolio_id=PF)
    assert sync.placed == ("e1:stop",) and sync.failed == ()
    assert current_state(state, "e1:stop") == "submitted"


# ---- cancelling at the broker -----------------------------------------------------------


def cancel_plan(stop=None, reason="position closed") -> StopPlan:
    return StopPlan(cancel=(StopCancel(stop or working(), reason),))


def test_cancelling_a_stop_the_ledger_never_had_is_done(state):
    sync = send_stop_plan(state, LookupBroker(), cancel_plan(), portfolio_id=PF)
    assert sync.cancelled == ("e1:stop",)


def test_an_unknown_stop_waits_for_reconciliation(state):
    paper_stop(state)
    mark_unknown(state, "e1:stop", "lost")
    broker = LookupBroker()
    sync = send_stop_plan(state, broker, cancel_plan(), portfolio_id=PF)
    assert sync.failed == ("e1:stop",) and broker.cancelled == []


def test_a_broker_that_cannot_cancel_leaves_the_stop_working(state):
    paper_stop(state)
    sync = send_stop_plan(state, PlainBroker(), cancel_plan(), portfolio_id=PF)
    assert sync.failed == ("e1:stop",)
    assert current_state(state, "e1:stop") == "accepted"


def test_a_cancel_with_no_answer_makes_the_stop_unknown(state):
    paper_stop(state)
    broker = LookupBroker(cancel_error=TimeoutError("gateway"))
    sync = send_stop_plan(state, broker, cancel_plan(), portfolio_id=PF)
    assert sync.failed == ("e1:stop",)
    assert current_state(state, "e1:stop") == "unknown"


def test_nothing_working_at_the_broker_cancels_the_row_with_the_reason(state):
    paper_stop(state)
    sync = send_stop_plan(state, CancelOnly(False), cancel_plan(reason="resized"), portfolio_id=PF)
    assert sync.cancelled == ("e1:stop",)
    assert current_state(state, "e1:stop") == "cancelled"
    [row] = state.sql("SELECT status_reason FROM orders WHERE client_id = 'e1:stop'")
    assert row["status_reason"] == "replaced by a stop for the new position size"


def test_a_requested_cancel_waits_for_the_broker_to_confirm(state):
    paper_stop(state)
    broker = LookupBroker(cancel=True, lookup_error=ConnectionError("dropped"))
    sync = send_stop_plan(state, broker, cancel_plan(), portfolio_id=PF)
    assert sync.cancelled == ("e1:stop",) and broker.cancelled == ["e1:stop"]
    assert current_state(state, "e1:stop") == "pending_cancel"


def test_a_confirmed_cancel_carries_the_reason(state):
    paper_stop(state)
    confirmed = BrokerOrderState(client_id="e1:stop", broker_order_id="b1", ticker="A.US",
                                 side="sell", status="cancelled", quantity=10.0,
                                 filled_quantity=0.0, avg_fill_price=None)  # fmt: skip
    broker = LookupBroker(cancel=True, known=confirmed)
    send_stop_plan(state, broker, cancel_plan(reason="stops turned off"), portfolio_id=PF)
    assert current_state(state, "e1:stop") == "cancelled"
    [row] = state.sql("SELECT status_reason FROM orders WHERE client_id = 'e1:stop'")
    assert row["status_reason"] == "protective stops were turned off"


# ---- a simulated book --------------------------------------------------------------------


def test_paper_plan_skips_cancels_it_cannot_apply_and_stops_it_already_has(state):
    paper_stop(state)
    done = record_paper_plan(
        state,
        StopPlan(place=(stop_order(),), cancel=(StopCancel(working("gone:stop"), "resized"),)),
        portfolio_id=PF,
    )
    assert done.placed == () and done.cancelled == ()
    write_state(state, "e1:stop", "filled")
    again = record_paper_plan(state, cancel_plan(), portfolio_id=PF)
    assert again.cancelled == ()  # it filled first
    assert current_state(state, "e1:stop") == "filled"


def test_no_lake_sweeps_nothing(state):
    paper_stop(state)
    assert sweep_paper_stops(state, None, Portfolio(cash=0.0, positions={"A.US": 10.0}),
                             portfolio_id=PF, as_of=AS_OF) == []  # fmt: skip


def test_a_stop_only_rests_from_the_day_after_it_was_placed(state, lake):
    paper_stop(state, stop_order("e1:stop", "A.US", price=90.0, decided=date(2026, 3, 10)))
    paper_stop(state, stop_order("e2:stop", "B.US", price=45.0, decided=date(2026, 3, 11)))
    lake.upsert_prices(pd.DataFrame({
        "ticker": ["A.US", "B.US", "A.US", "B.US"],
        "date": pd.to_datetime(["2026-03-11", "2026-03-11", "2026-03-12", "2026-03-12"]),
        "open": [95.0, 50.0, 95.0, 50.0],
        "high": [96.0, 51.0, 96.0, 51.0],
        # B.US trades through its stop on the day it was placed, then holds
        "low": [94.0, 40.0, 94.0, float("nan")],
        "close": [95.0, 50.0, 95.0, 50.0], "adj_close": 1.0, "volume": 100.0,
    }))  # fmt: skip
    portfolio = Portfolio(cash=0.0, positions={"A.US": 10.0, "B.US": 10.0})
    assert sweep_paper_stops(state, lake, portfolio, portfolio_id=PF,
                             as_of=date(2026, 3, 12)) == []  # fmt: skip


# ---- the live_stops job ----------------------------------------------------------------------


def book(pid=PF) -> LiveBook:
    return LiveBook(portfolio_id=pid, settings_for=lambda _sid: ON, enabled=True)


def test_the_job_needs_a_broker_that_looks_orders_up(state):
    out = sync_live_books(state, None, [book()], lambda _pid: PlainBroker(), as_of=AS_OF)
    assert out == {PF: {"error": "the broker cannot look orders up by client id"}}


def test_one_book_failing_to_open_is_reported(state):
    def boom(_pid):
        raise ConnectionError("gateway down")

    out = sync_live_books(state, None, [book()], boom, as_of=AS_OF)
    assert out == {PF: {"error": "ConnectionError: gateway down"}}


def test_a_book_with_stops_off_and_none_working_is_left_alone(state):
    off = LiveBook(portfolio_id=PF, settings_for=lambda _sid: ProtectiveStopSettings(),
                   enabled=False)  # fmt: skip
    assert sync_live_books(state, None, [off], lambda _pid: PlainBroker(), as_of=AS_OF) == {}
