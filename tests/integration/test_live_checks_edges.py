"""Edges of the reconciliation checks (roadmap 19.5 and 19.15): stale
orders the start-of-day check cannot cancel, a broker that cannot cancel
or list orders, state DBs without the report or halt tables, alerts that
fail to send, and the statement cash flows the end-of-day check counts.
A real state DB, small fakes and ``FakeIbGateway``. No network."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from stonks.core.clock import FakeClock
from stonks.core.types import Order, Portfolio
from stonks.execution.brokers.base import BrokerError, BrokerUnavailableError
from stonks.execution.drift import BrokerStatement, StatementCash, StatementExecution
from stonks.execution.order_state import write_state
from stonks.production.live.checks import (
    _statement_flows,
    check_kind,
    list_reports,
    run_check,
)
from stonks.production.live.settings import LiveSettings, ReconcileSettings
from tests.fakes.ib_gateway import AAPL, FakeIbGateway
from tests.integration.test_live_checks import CID, MON, PF, bought, ib_broker, insert_order
from tests.integration.test_live_checks_cash import (
    MON_EOD,
    TUE_EOD,
    eod,
    monday_baseline,
    set_cash,
)

YESTERDAY = MON - timedelta(days=1)


class LedgerBroker:
    """A broker that holds ``positions``, reports no fills and optionally
    cancels (``cancel`` is what ``cancel_order`` does: a bool or an error)."""

    def __init__(self, positions=None) -> None:
        self.positions = dict(positions or {})

    def fetch_portfolio(self) -> Portfolio:
        return Portfolio(cash=0.0, positions=dict(self.positions))

    def place_order(self, order: Order):
        return None

    def reconcile(self):
        return []


class CancellingBroker(LedgerBroker):
    def __init__(self, cancel: bool | Exception = True, positions=None) -> None:
        super().__init__(positions)
        self.cancel = cancel
        self.asked: list[str] = []

    def cancel_order(self, client_id: str) -> bool:
        self.asked.append(client_id)
        if isinstance(self.cancel, Exception):
            raise self.cancel
        return self.cancel


def stale_order(state, client_id=CID, fine="accepted"):
    insert_order(state, client_id, created=YESTERDAY)
    state.execute("UPDATE orders SET state = ? WHERE client_id = ?", [fine, client_id])


def sod(state, broker, sent=None, **kw):
    return run_check(state, broker, PF, "sod", clock=FakeClock(MON),
                     publish=(sent.append if sent is not None else lambda e: None),
                     **kw)  # fmt: skip


# ---- start of day: stale orders -------------------------------------------------------


def test_a_broker_that_cannot_cancel_leaves_stale_orders_alone(state):
    stale_order(state)
    result = sod(state, LedgerBroker())
    assert result.report.explained == ()


def test_only_orders_the_broker_works_are_cancelled(state):
    stale_order(state, "never-sent", fine="pending")
    stale_order(state, "working")
    broker = CancellingBroker(True)
    result = sod(state, broker)
    assert broker.asked == ["working"]
    [item] = result.report.explained
    assert (item.kind, item.key, item.explained) == ("stale_order", "working", True)
    # a broker that cannot report order states leaves the ledger's view
    assert item.broker == "accepted"


def test_a_refused_cancel_is_reported_not_explained(state):
    stale_order(state)
    result = sod(state, CancellingBroker(BrokerError("order is locked")))
    [item] = result.report.explained
    assert (item.kind, item.explained, item.material) == ("stale_order", False, False)
    assert "the cancel failed: order is locked" in item.detail
    assert result.status == "clean"


def test_a_cancel_the_broker_does_not_answer_is_an_outage(state):
    stale_order(state)
    result = sod(state, CancellingBroker(BrokerUnavailableError("gateway down")))
    assert result.status == "outage" and not result.may_submit
    assert "gateway down" in (result.report.detail or "")


def test_stale_orders_can_be_left_working(state):
    stale_order(state)
    broker = CancellingBroker(True)
    sod(state, broker, settings=LiveSettings(cancel_stale_orders=False))
    assert broker.asked == []


# ---- the broker still works an order the ledger closed ------------------------------


def test_an_order_the_ledger_closed_but_the_broker_works_is_compared(state):
    gw = FakeIbGateway()
    broker = ib_broker(state, gw)
    bought(state, gw, broker, fill=False)
    state.execute("UPDATE orders SET status = 'cancelled', state = 'cancelled'"
                  " WHERE client_id = ?", [CID])  # fmt: skip
    result = run_check(state, broker, PF, "adhoc", clock=FakeClock(MON), publish=lambda e: None)
    assert CID in {i.key for i in result.report.items}
    assert result.status == "drift"


# ---- older state DBs ------------------------------------------------------------------


def test_a_state_db_without_report_tables_still_checks(state):
    state.execute("DROP TABLE reconcile_reports")
    insert_order(state)
    state.execute("UPDATE orders SET status = 'filled', state = 'filled'")
    state.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at, portfolio_id)"
        " VALUES (?, 'AAPL.US', 10, 200.0, 0.0, ?, ?)",
        [CID, MON.isoformat(), PF],
    )
    result = run_check(state, LedgerBroker({"AAPL.US": 4.0}), PF, "adhoc", clock=FakeClock(MON),
                       publish=lambda e: None)  # fmt: skip
    assert result.status == "drift"
    assert result.report.summary["drift_streak"] == 1
    assert list_reports(state) == []


def test_drift_without_the_halt_table_opens_no_halt(state):
    for table in ("reconcile_reports", "risk_halts"):  # reports point at halts
        state.execute(f"DROP TABLE {table}")
    insert_order(state)
    state.execute("UPDATE orders SET status = 'filled', state = 'filled'")
    state.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at, portfolio_id)"
        " VALUES (?, 'AAPL.US', 10, 200.0, 0.0, ?, ?)",
        [CID, MON.isoformat(), PF],
    )
    result = run_check(state, LedgerBroker(), PF, "adhoc", clock=FakeClock(MON),
                       publish=lambda e: None)  # fmt: skip
    assert result.status == "drift"
    assert result.report.halt_id is None and not result.halt_created


# ---- alerts ----------------------------------------------------------------------------


def test_an_alert_that_fails_to_send_never_fails_the_check(state):
    def broken(_event):
        raise RuntimeError("push service down")

    stale_order(state)
    result = run_check(state, CancellingBroker(BrokerUnavailableError("down")), PF, "sod",
                       clock=FakeClock(MON), publish=broken)  # fmt: skip
    assert result.status == "outage"
    assert [r.id for r in list_reports(state)] == [result.report.id]


# ---- end of day: cash and statements ----------------------------------------------------


def test_a_statement_without_dates_is_not_compared(state):
    gw = FakeIbGateway()
    broker = ib_broker(state, gw, now=TUE_EOD)
    undated = BrokerStatement(account_id="DU1234567", from_date=None, to_date=None,
                              executions=(StatementExecution("x.1", 10.0, "AAPL",
                                                             order_ref=CID),))  # fmt: skip
    settings = LiveSettings(reconcile=ReconcileSettings(compare_cash=False))
    result = eod(state, broker, TUE_EOD, settings=settings, statements=lambda: [undated])
    assert result.status == "clean"
    assert result.report.summary["statement"] == {"status": "ok", "statements": 1}


def test_a_new_base_currency_starts_a_new_baseline(state):
    gw = FakeIbGateway()
    broker, first = monday_baseline(state, gw)
    summary = dict(first.report.summary)
    summary["cash"] = {**summary["cash"], "currency": "EUR"}
    state.execute("UPDATE reconcile_reports SET summary_json = ? WHERE id = ?",
                  [json.dumps(summary), first.report.id])  # fmt: skip
    set_cash(gw, 10_000)  # would be a large difference against the old baseline
    result = eod(state, broker, TUE_EOD)
    assert result.status == "clean"
    cash = result.report.summary["cash"]
    assert cash["compared"] is False and "base currency changed" in cash["reason"]


def test_an_end_of_day_check_without_cash_is_skipped_as_a_baseline(state):
    gw = FakeIbGateway()
    broker, _ = monday_baseline(state, gw)
    no_cash = LiveSettings(reconcile=ReconcileSettings(compare_cash=False))
    eod(state, broker, TUE_EOD, settings=no_cash)
    wed = TUE_EOD + timedelta(days=1)
    result = eod(state, ib_broker(state, gw, now=wed), wed)
    cash = result.report.summary["cash"]
    assert cash["compared"] is True and cash["since"] == MON_EOD.isoformat(timespec="seconds")


def test_fills_before_the_baseline_are_not_counted_again(state):
    gw = FakeIbGateway()
    broker = ib_broker(state, gw, now=MON_EOD)
    insert_order(state, created=MON)
    broker.place_order(Order(client_id=CID, ticker="AAPL.US", side="buy", quantity=10,
                             decision_price=200.0))  # fmt: skip
    write_state(state, CID, "submitted")
    gw.fill(CID, 10, 200.0, commission=1.0, at=MON + timedelta(minutes=1))
    gw.set_position(AAPL, 10)
    set_cash(gw, 47_999, settled=50_000)
    eod(state, broker, MON_EOD)  # the baseline already holds Monday's buy
    tuesday = ib_broker(state, gw, now=TUE_EOD)
    set_cash(gw, 47_999, settled=47_999)  # it settles on Tuesday
    result = eod(state, tuesday, TUE_EOD)
    assert result.status == "clean", result.report.items
    assert result.report.summary["cash"]["flows"]["trades"] == 0.0


# ---- statement cash flows ----------------------------------------------------------------


D0, D1, D2 = date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)


def flows(*statements, owned=frozenset({"AAPL"}), refs=frozenset({CID}),
          booked=frozenset({"ours.1"})):  # fmt: skip
    return _statement_flows(list(statements), "USD", D0, D1, set(owned), set(refs), set(booked))


def test_stonks_own_executions_are_left_to_the_ledger():
    s = BrokerStatement("A", D1, D1, executions=(
        StatementExecution("ours.1", 10.0, "AAPL", cash=-2000.0),
        StatementExecution("ours.2", 10.0, "AAPL", order_ref=CID, cash=-2000.0),
    ))  # fmt: skip
    assert flows(s) == (0.0, 0.0, 0.0, 0.0, 0)


def test_hand_trades_count_once_and_settle_on_their_settle_date():
    hand = StatementExecution("hand.1", 5.0, "MSFT", trade_date=D1, settle_date=D2,
                              cash=-1000.0, commission=1.0)  # fmt: skip
    s = BrokerStatement("A", D1, D1, executions=(hand,))
    # the same execution in two statements counts once; it settles after ``day``
    assert flows(s, s) == (0.0, -1001.0, 0.0, 0.0, 0)


def test_rows_in_another_currency_are_skipped():
    s = BrokerStatement(
        "A", D1, D1,
        executions=(StatementExecution("hand.2", 1.0, "SAP", trade_date=D1, cash=-100.0,
                                       currency="EUR"),),
        cash=(StatementCash("other", 50.0, D1, currency="EUR"),),
    )  # fmt: skip
    assert flows(s) == (0.0, 0.0, 0.0, 0.0, 2)


def test_dividends_on_stonks_positions_are_told_apart_from_other_cash():
    s = BrokerStatement("A", D1, D1, cash=(
        StatementCash("dividend", 3.0, D1, symbol="aapl"),
        StatementCash("dividend", 3.0, D1, symbol="aapl"),  # a repeat row
        StatementCash("dividend", 2.0, D1, symbol="MSFT"),  # the owner's own holding
        StatementCash("other", -7.0, D1, settle_date=D2),  # settles after ``day``
        StatementCash("other", 9.0, D0),  # before the window
        StatementCash("dividend", 4.0, D0, settle_date=D1, symbol="AAPL"),  # settles inside
    ))  # fmt: skip
    dividends, external, settled_div, settled_ext, skipped = flows(s)
    assert (dividends, external) == (3.0, pytest.approx(-5.0))
    assert (settled_div, settled_ext, skipped) == (7.0, 2.0, 0)


# ---- kinds ---------------------------------------------------------------------------------


def test_check_kind_accepts_only_known_kinds():
    assert check_kind("eod") == "eod"
    with pytest.raises(ValueError, match="kind must be one of"):
        check_kind("weekly")
