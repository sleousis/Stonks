"""Deeper reconciliation (roadmap 19.15): cash, settled cash and the broker's
statement in the end-of-day check. A real state DB and ``FakeIbGateway``.
No network."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from stonks.core.clock import FakeClock
from stonks.core.types import Order
from stonks.execution.drift import BrokerStatement, StatementCash, StatementExecution
from stonks.execution.order_state import write_state
from stonks.production.halts import active_halts
from stonks.production.live.checks import run_check
from stonks.production.live.settings import LiveSettings, ReconcileSettings
from tests.fakes.ib_gateway import AAPL, FakeIbGateway
from tests.integration.test_live_checks import CID, MON, PF, TUE, ib_broker, insert_order

MON_EOD = MON + timedelta(hours=7)  # 20:30 UTC, after the close
TUE_EOD = TUE + timedelta(hours=7)
TUE_FILL = TUE + timedelta(minutes=1)


def eod(state, broker, now, *, settings=None, statements=None, sent=None, **kw):
    return run_check(
        state,
        broker,
        PF,
        "eod",
        clock=FakeClock(now),
        settings=settings,
        statements=statements,
        publish=(sent.append if sent is not None else lambda e: None),
        **kw,
    )


def set_cash(gw, cash, settled=None):
    gw.set_values(TotalCashValue=str(cash), SettledCash=str(cash if settled is None else settled))


def buy_tuesday(state, gw, broker, *, qty=10, price=200.0, commission=1.0) -> str:
    """Stonks bought ``qty`` AAPL on Tuesday. Returns the execution id."""
    insert_order(state, CID, created=TUE, qty=qty)
    broker.place_order(
        Order(client_id=CID, ticker="AAPL.US", side="buy", quantity=qty, decision_price=price)
    )
    write_state(state, CID, "submitted")
    exec_id = gw.fill(CID, qty, price, commission=commission, at=TUE_FILL)
    gw.set_position(AAPL, qty)
    return exec_id


@pytest.fixture
def gw():
    return FakeIbGateway()


def monday_baseline(state, gw):
    broker = ib_broker(state, gw, now=MON_EOD)
    set_cash(gw, 50_000)
    first = eod(state, broker, MON_EOD)
    return broker, first


# ---- cash ------------------------------------------------------------------------------------


def test_the_first_eod_check_stores_the_cash_baseline(state, gw):
    _, first = monday_baseline(state, gw)
    assert first.status == "clean"
    cash = first.report.summary["cash"]
    assert (cash["currency"], cash["cash"], cash["settled_cash"], cash["compared"]) == (
        "USD",
        50_000.0,
        50_000.0,
        False,
    )


def test_cash_that_stonks_explains_is_clean(state, gw):
    broker, _ = monday_baseline(state, gw)
    buy_tuesday(state, gw, broker)
    set_cash(gw, 47_999, settled=50_000)  # the buy settles on Wednesday (T+1)

    result = eod(state, broker, TUE_EOD)
    assert result.status == "clean", result.report.items
    cash = result.report.summary["cash"]
    assert cash["compared"] is True
    assert cash["flows"]["trades"] == pytest.approx(-2000.0)
    assert cash["flows"]["fees"] == pytest.approx(-1.0)


def test_unexplained_cash_warns_and_never_halts_by_default(state, gw):
    broker, _ = monday_baseline(state, gw)
    buy_tuesday(state, gw, broker)
    set_cash(gw, 47_500, settled=50_000)
    sent = []

    result = eod(state, broker, TUE_EOD, sent=sent)
    assert result.status == "warn" and result.may_submit
    [item] = result.report.items
    assert (item.kind, item.key, item.material) == ("cash", "USD", False)
    assert item.broker == pytest.approx(-2500.0) and item.ours == pytest.approx(-2001.0)
    assert not active_halts(state, TUE.date(), portfolio_id=PF)
    assert len(sent) == 1 and sent[0].level == "warning"


def test_settled_cash_follows_the_settlement_date(state, gw):
    broker, _ = monday_baseline(state, gw)
    buy_tuesday(state, gw, broker)
    set_cash(gw, 47_999, settled=47_999)  # settled at once: not what T+1 says

    result = eod(state, broker, TUE_EOD)
    [item] = result.report.items
    assert (item.kind, item.ours, item.broker) == ("settled_cash", 0.0, pytest.approx(-2001.0))


def test_a_cash_difference_is_drift_when_configured(state, gw):
    broker, _ = monday_baseline(state, gw)
    set_cash(gw, 49_000)
    settings = LiveSettings(reconcile=ReconcileSettings(cash_is_drift=True))

    result = eod(state, broker, TUE_EOD, settings=settings)
    assert result.status == "drift"
    assert {i.kind for i in result.report.items} == {"cash", "settled_cash"}
    assert active_halts(state, TUE.date(), portfolio_id=PF)


def test_the_cash_comparison_can_be_turned_off(state, gw):
    broker, _ = monday_baseline(state, gw)
    set_cash(gw, 10_000)
    settings = LiveSettings(reconcile=ReconcileSettings(compare_cash=False))
    result = eod(state, broker, TUE_EOD, settings=settings)
    assert result.status == "clean" and "cash" not in result.report.summary


def test_a_difference_inside_the_tolerance_is_clean(state, gw):
    broker, _ = monday_baseline(state, gw)
    set_cash(gw, 49_991)  # tolerance: 0.01% of 100 000 = 10
    assert eod(state, broker, TUE_EOD).status == "clean"


def test_flows_in_the_statement_explain_the_owners_cash(state, gw):
    broker, _ = monday_baseline(state, gw)
    buy_tuesday(state, gw, broker)
    set_cash(gw, 47_999 + 5_000 + 3.0 - 1_001.0, settled=50_000 + 5_000 + 3.0)
    tuesday = TUE.date()
    statement = BrokerStatement(
        account_id="DU1234567",
        from_date=tuesday,
        to_date=tuesday,
        executions=(
            # the owner bought MSFT by hand: no reference of ours
            StatementExecution(
                exec_id="hand.1",
                quantity=5,
                symbol="MSFT",
                trade_date=tuesday,
                settle_date=date(2026, 9, 30),
                cash=-1000.0,
                commission=1.0,
            ),
        ),
        cash=(
            StatementCash(kind="other", amount=5000.0, date=tuesday, symbol=None),
            StatementCash(kind="dividend", amount=3.0, date=tuesday, symbol="MSFT"),
        ),
    )
    settings = LiveSettings(reconcile=ReconcileSettings(compare_statement=False))

    result = eod(state, broker, TUE_EOD, settings=settings, statements=lambda: [statement])
    assert result.status == "clean", result.report.items
    flows = result.report.summary["cash"]["flows"]
    assert flows["external"] == pytest.approx(5000.0 + 3.0 - 1001.0)


# ---- broker statement ------------------------------------------------------------------------


def ours(exec_id, *, qty=10.0, commission=1.0):
    return StatementExecution(
        exec_id=exec_id,
        quantity=qty,
        symbol="AAPL",
        order_ref=CID,
        trade_date=TUE.date(),
        cash=-qty * 200.0,
        commission=commission,
        commission_currency="USD",
    )


def statement_of(*executions):
    return BrokerStatement(
        account_id="DU1234567",
        from_date=TUE.date(),
        to_date=TUE.date(),
        executions=tuple(executions),
    )


NO_CASH = LiveSettings(reconcile=ReconcileSettings(compare_cash=False))


def test_a_matching_statement_is_clean(state, gw):
    broker = ib_broker(state, gw, now=TUE_EOD)
    exec_id = buy_tuesday(state, gw, broker)
    result = eod(
        state, broker, TUE_EOD, settings=NO_CASH, statements=lambda: [statement_of(ours(exec_id))]
    )
    assert result.status == "clean", result.report.items
    assert result.report.summary["statement"] == {"status": "ok", "statements": 1}


def test_an_execution_missing_from_the_ledger_is_drift(state, gw):
    broker = ib_broker(state, gw, now=TUE_EOD)
    exec_id = buy_tuesday(state, gw, broker)
    lost = ours("0001.9999", qty=5.0)
    result = eod(
        state,
        broker,
        TUE_EOD,
        settings=NO_CASH,
        statements=lambda: [statement_of(ours(exec_id), lost)],
    )
    assert result.status == "drift"
    assert [(i.kind, i.key) for i in result.report.items] == [
        ("statement_missing_execution", "0001.9999")
    ]


def test_a_booked_fill_the_statement_lacks_is_drift(state, gw):
    broker = ib_broker(state, gw, now=TUE_EOD)
    exec_id = buy_tuesday(state, gw, broker)
    result = eod(state, broker, TUE_EOD, settings=NO_CASH, statements=lambda: [statement_of()])
    assert result.status == "drift"
    assert [(i.kind, i.key) for i in result.report.items] == [
        ("statement_extra_execution", exec_id)
    ]


def test_a_commission_difference_warns(state, gw):
    broker = ib_broker(state, gw, now=TUE_EOD)
    exec_id = buy_tuesday(state, gw, broker, commission=1.0)
    result = eod(
        state,
        broker,
        TUE_EOD,
        settings=NO_CASH,
        statements=lambda: [statement_of(ours(exec_id, commission=1.35))],
    )
    assert result.status == "warn"
    [item] = result.report.items
    assert (item.kind, item.ours, item.broker) == ("statement_commission", 1.0, 1.35)


def test_the_hand_trades_and_other_accounts_in_a_statement_are_ignored(state, gw):
    broker = ib_broker(state, gw, now=TUE_EOD)
    exec_id = buy_tuesday(state, gw, broker)
    hand = StatementExecution(exec_id="hand.1", quantity=5, symbol="MSFT", trade_date=TUE.date())
    other = BrokerStatement("U999", TUE.date(), TUE.date(), (ours("x.1"),))
    result = eod(
        state,
        broker,
        TUE_EOD,
        settings=NO_CASH,
        account_id="DU1234567",
        statements=lambda: [statement_of(ours(exec_id), hand), other],
    )
    assert result.status == "clean", result.report.items


def test_fills_outside_the_statement_days_are_not_compared(state, gw):
    broker = ib_broker(state, gw, now=TUE_EOD)
    buy_tuesday(state, gw, broker)
    monday_only = BrokerStatement("DU1234567", MON.date(), MON.date(), ())
    result = eod(state, broker, TUE_EOD, settings=NO_CASH, statements=lambda: [monday_only])
    assert result.status == "clean", result.report.items


def test_a_statement_that_fails_never_fails_the_check(state, gw):
    broker = ib_broker(state, gw, now=TUE_EOD)
    buy_tuesday(state, gw, broker)

    def broken():
        raise RuntimeError("Flex request failed: 503")

    result = eod(state, broker, TUE_EOD, settings=NO_CASH, statements=broken)
    assert result.status == "clean"
    note = result.report.summary["statement"]
    assert note["status"] == "failed" and "503" in note["error"]


def test_the_statement_is_only_read_at_the_end_of_the_day(state, gw):
    broker = ib_broker(state, gw, now=TUE_EOD)
    buy_tuesday(state, gw, broker)
    calls = []
    run_check(
        state,
        broker,
        PF,
        "sod",
        clock=FakeClock(TUE_EOD),
        publish=lambda e: None,
        statements=lambda: calls.append(1) or [],
    )
    assert calls == []


# ---- drift that stays ------------------------------------------------------------------------


def test_drift_in_a_row_is_counted_for_the_stage_gate(state, gw):
    broker = ib_broker(state, gw, now=TUE_EOD)
    exec_id = buy_tuesday(state, gw, broker)
    statements = lambda: [statement_of()]  # noqa: E731
    first = eod(state, broker, TUE_EOD, settings=NO_CASH, statements=statements)
    second = eod(state, broker, TUE_EOD, settings=NO_CASH, statements=statements)
    assert first.report.summary["drift_streak"] == 1
    assert second.report.summary["drift_streak"] == 2
    clean = eod(
        state, broker, TUE_EOD, settings=NO_CASH, statements=lambda: [statement_of(ours(exec_id))]
    )
    assert clean.status == "clean" and "drift_streak" not in clean.report.summary
