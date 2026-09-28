"""Reconciliation checks of a live portfolio (roadmap 19.5): a real state DB,
``FakeIbGateway`` for IBKR and the simulated broker. No network."""

from __future__ import annotations

from datetime import timedelta

import pytest

from stonks.core.clock import FakeClock
from stonks.core.types import Order, Portfolio
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.execution.brokers.ibkr.contracts import ContractResolver, SqliteContractCache
from stonks.execution.order_state import current_state, write_state
from stonks.production.halts import active_halts
from stonks.production.live.checks import get_report, list_reports, run_check, submit_gate
from stonks.production.live.settings import LiveSettings
from tests.fakes.ib_gateway import AAPL, MSFT, T0, FakeIbGateway

PF = "pf_default"
MON = T0  # 2026-09-28 13:30 UTC
TUE = T0 + timedelta(days=1)
CID = "t1-s1-AAPL.US-buy"


@pytest.fixture
def sent():
    return []


def auto_subscription(state) -> str:
    state.execute(
        "INSERT INTO strategies (id, class_path, params_json, status, created_at, updated_at)"
        " VALUES ('s1', 'x.Y', '{}', 'active', 'x', 'x')"
    )
    state.execute(
        "INSERT INTO subscriptions (id, user_id, strategy_id, portfolio_id, mode, weight,"
        " created_at, updated_at) SELECT 'sub_1', owner_id, 's1', id, 'auto', 1.0, 'x', 'x'"
        " FROM portfolios WHERE id = ?",
        [PF],
    )
    return "sub_1"


def paused_reason(state, sub_id="sub_1"):
    return state.sql("SELECT paused_reason FROM subscriptions WHERE id = ?", [sub_id])[0][0]


def insert_order(state, client_id=CID, *, created=MON, tif=None, qty=10, ticker="AAPL.US"):
    state.execute(
        "INSERT INTO orders (client_id, tick_id, strategy_id, ticker, side, quantity, order_type,"
        " limit_price, status, state, broker_order_id, created_at, updated_at, portfolio_id,"
        " time_in_force) VALUES (?, NULL, NULL, ?, 'buy', ?, 'market', NULL, 'pending',"
        " 'pending', NULL, ?, ?, ?, ?)",
        [client_id, ticker, qty, created.isoformat(), created.isoformat(), PF, tif],
    )


def ib_broker(state, gw, now=MON) -> IbkrBroker:
    gw.set_values(NetLiquidation="100000", TotalCashValue="50000")
    resolver = ContractResolver(gw, cache=SqliteContractCache(state), clock=FakeClock(T0))
    return IbkrBroker(gw, mode="paper", resolver=resolver, clock=FakeClock(now))


def bought(state, gw, broker, *, client_id=CID, qty=10, created=MON, fill=True):
    """Stonks bought ``qty`` AAPL through the broker (the tick's path)."""
    insert_order(state, client_id, created=created, qty=qty)
    broker.place_order(
        Order(client_id=client_id, ticker="AAPL.US", side="buy", quantity=qty, decision_price=200.0)
    )
    write_state(state, client_id, "submitted")
    if fill:
        gw.fill(client_id, qty, 200.0, commission=1.0)


def check(state, broker, kind="adhoc", *, now=MON, sent=None, **kw):
    return run_check(
        state,
        broker,
        PF,
        kind,
        clock=FakeClock(now),
        publish=(sent.append if sent is not None else lambda e: None),
        **kw,
    )


# ---- clean and external ------------------------------------------------------------------


def test_a_matching_account_is_clean_and_stored(state, sent):
    gw = FakeIbGateway()
    broker = ib_broker(state, gw)
    bought(state, gw, broker)
    gw.set_position(AAPL, 10)

    result = check(state, broker, sent=sent)
    assert result.status == "clean" and result.may_submit
    assert result.report.summary["fills_booked"] == 1
    assert current_state(state, CID) == "filled"
    stored = get_report(state, result.report.id)
    assert stored == result.report
    assert not active_halts(state, MON.date(), portfolio_id=PF)
    assert sent == []


def test_the_owners_own_positions_and_orders_are_external_not_drift(state, sent):
    gw = FakeIbGateway()
    broker = ib_broker(state, gw)
    bought(state, gw, broker)
    gw.set_position(AAPL, 25)  # the owner bought 15 more by hand
    gw.set_position(MSFT, 3)
    manual = gw.add_manual_order(MSFT, 5)

    result = check(state, broker, sent=sent)
    assert result.status == "clean"
    external = result.report.external
    assert external["positions"] == {"AAPL.US": 15.0, "MSFT.US": 3.0}
    assert [o["broker_order_id"] for o in external["orders"]] == [str(manual.perm_id)]


# ---- drift ----------------------------------------------------------------------------------


def test_drift_opens_the_halt_pauses_auto_and_alerts(state, sent):
    auto_subscription(state)
    gw = FakeIbGateway()
    broker = ib_broker(state, gw)
    bought(state, gw, broker)
    gw.set_position(AAPL, 9)  # one share gone

    result = check(state, broker, "sod", sent=sent)
    report = result.report
    assert result.status == "drift" and not result.may_submit
    [item] = report.items
    assert (item.kind, item.key, item.ours, item.broker) == ("position_qty", "AAPL.US", 10, 9)
    [halt] = active_halts(state, MON.date(), portfolio_id=PF)
    assert halt.kind == "broker_drift" and halt.halt == "buys" and halt.id == report.halt_id
    assert report.id in halt.reason
    assert report.paused == ("sub_1",)
    assert paused_reason(state).startswith("broker_drift: ") and report.id in paused_reason(state)
    assert [e.title for e in sent] == ["Trading stopped: broker drift"]

    # a second check finds the same halt open and pauses nothing new
    again = check(state, broker, "eod", sent=sent)
    assert again.status == "drift" and again.report.halt_id == halt.id
    assert again.report.paused == () and not again.halt_created
    assert len(active_halts(state, MON.date(), portfolio_id=PF)) == 1


def test_manual_trades_off_make_a_hand_placed_order_drift(state):
    gw = FakeIbGateway()
    broker = ib_broker(state, gw)
    manual = gw.add_manual_order(MSFT, 5)
    result = check(state, broker, settings=LiveSettings(allow_manual_trades=False))
    assert result.status == "drift"
    assert [(i.kind, i.key) for i in result.report.items] == [
        ("unknown_order", str(manual.perm_id))
    ]


def test_a_working_order_the_broker_lost_is_drift(state):
    gw = FakeIbGateway()
    broker = ib_broker(state, gw)
    insert_order(state)
    state.execute(
        "UPDATE orders SET state = 'accepted', broker_order_id = '424242' WHERE client_id = ?",
        [CID],
    )
    result = check(state, broker)
    assert result.status == "drift"
    kinds = {(i.kind, i.key) for i in result.report.items}
    assert ("missing_order", CID) in kinds


def test_a_fill_that_lands_after_the_first_read_is_booked_not_drift(state):
    """The check reconciles again before it calls a difference drift."""
    gw = FakeIbGateway()
    broker = ib_broker(state, gw)
    bought(state, gw, broker, fill=False)
    gw.set_position(AAPL, 10)

    original = broker.fetch_portfolio
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        if calls["n"] == 1:
            gw.fill(CID, 10, 200.0, commission=1.0)  # the fill arrives mid-check
        return original()

    broker.fetch_portfolio = fetch  # type: ignore[method-assign]
    result = check(state, broker)
    # first look: the broker holds 10, the ledger none, and an open order the
    # broker no longer works. The recheck books the fill.
    assert result.status == "clean"
    assert "recheck" in result.report.summary


# ---- outage and fault ------------------------------------------------------------------------


def down_gateway() -> FakeIbGateway:
    gw = FakeIbGateway()
    gw.connect_failures = 10**6
    return gw


def test_a_short_outage_skips_and_alerts_but_does_not_pause(state, sent):
    auto_subscription(state)
    broker = ib_broker(state, down_gateway())
    result = check(state, broker, "sod", sent=sent)
    assert result.status == "outage" and not result.may_submit
    assert result.report.detail and "BrokerUnavailableError" in result.report.detail
    assert result.report.paused == () and paused_reason(state) is None
    assert [e.urgency for e in sent] == ["high"]
    # a second failure on the same session still counts one session
    assert check(state, broker, "eod", sent=sent).report.paused == ()


def test_an_outage_on_two_sessions_in_a_row_pauses(state, sent):
    auto_subscription(state)
    broker = ib_broker(state, down_gateway())
    check(state, broker, "sod", now=MON, sent=sent)
    result = check(state, broker, "sod", now=TUE, sent=sent)
    assert result.report.summary["outage_sessions"] == 2
    assert result.report.paused == ("sub_1",)
    assert paused_reason(state).startswith("broker_error: ")


def test_a_good_check_between_outages_resets_the_count(state):
    auto_subscription(state)
    gw = FakeIbGateway()
    broker = ib_broker(state, gw)
    gw.connect_failures = 10**6
    check(state, broker, "sod", now=MON)
    gw.connect_failures = 0
    assert check(state, broker, "eod", now=MON).status == "clean"
    gw.connected = False
    gw.connect_failures = 10**6
    result = check(state, broker, "sod", now=TUE)
    assert result.status == "outage" and result.report.paused == ()


def test_a_wrong_account_is_a_fault_that_pauses_at_once(state, sent):
    auto_subscription(state)
    broker = ib_broker(state, FakeIbGateway(["U7654321"]))  # a live account on paper
    result = check(state, broker, sent=sent)
    assert result.status == "fault"
    assert result.report.paused == ("sub_1",)
    assert paused_reason(state).startswith("broker_error: ")
    assert not active_halts(state, MON.date(), portfolio_id=PF)


def test_a_fault_pauses_approve_subscriptions_too(state, sent):
    """Approve mode trades at the broker too (roadmap 19.8): a fault pauses
    it with auto, as the tick and the broker health job do."""
    auto_subscription(state)
    state.execute("UPDATE subscriptions SET mode = 'approve' WHERE id = 'sub_1'")
    broker = ib_broker(state, FakeIbGateway(["U7654321"]))  # a live account on paper
    result = check(state, broker, sent=sent)
    assert result.status == "fault"
    assert result.report.paused == ("sub_1",)
    assert paused_reason(state).startswith("broker_error: ")


# ---- start and end of day --------------------------------------------------------------------


def test_sod_cancels_yesterdays_working_orders(state):
    gw = FakeIbGateway()
    broker = ib_broker(state, gw)
    yesterday = MON - timedelta(days=1)
    bought(state, gw, broker, created=yesterday, fill=False)
    state.execute("UPDATE orders SET time_in_force = 'opg' WHERE client_id = ?", [CID])

    result = check(state, broker, "sod", now=MON)
    assert result.status == "clean"
    [item] = result.report.explained
    assert (item.kind, item.key, item.explained) == ("stale_order", CID, True)
    assert gw.cancels  # the gateway was asked
    assert current_state(state, CID) in ("cancelled", "expired")


def test_sod_leaves_gtc_orders_alone(state):
    gw = FakeIbGateway()
    broker = ib_broker(state, gw)
    bought(state, gw, broker, created=MON - timedelta(days=1), fill=False)
    state.execute("UPDATE orders SET time_in_force = 'gtc' WHERE client_id = ?", [CID])
    result = check(state, broker, "sod", now=MON)
    assert result.report.explained == () and not gw.cancels


def test_eod_warns_on_a_stuck_order_and_a_missing_commission(state, sent):
    gw = FakeIbGateway()
    broker = ib_broker(state, gw)
    bought(state, gw, broker, fill=False)  # still working after the close
    insert_order(state, "t1-s1-AAPL.US-buy-2", qty=5)
    broker.place_order(
        Order(
            client_id="t1-s1-AAPL.US-buy-2",
            ticker="AAPL.US",
            side="buy",
            quantity=5,
            decision_price=200.0,
        )
    )
    write_state(state, "t1-s1-AAPL.US-buy-2", "submitted")
    gw.fill("t1-s1-AAPL.US-buy-2", 5, 200.0)  # no commission yet
    gw.set_position(AAPL, 5)

    eod = MON.replace(hour=20, minute=15)
    result = check(state, broker, "eod", now=eod, sent=sent)
    assert result.status == "warn" and result.may_submit
    kinds = sorted(i.kind for i in result.report.items)
    assert kinds == ["commission_missing", "stuck_order"]
    [event] = sent
    assert event.urgency == "high" and event.title == "Stuck order at the close"


# ---- submit gate ------------------------------------------------------------------------------


def test_the_submit_gate_waits_for_an_unresolved_order(state):
    gw = FakeIbGateway()
    broker = ib_broker(state, gw)
    insert_order(state)
    state.execute(
        "UPDATE orders SET state = 'unknown', broker_order_id = '99' WHERE client_id = ?", [CID]
    )
    result = submit_gate(state, broker, PF, clock=FakeClock(MON), publish=lambda e: None)
    assert result.report.kind == "submit"
    assert result.status == "warn" and not result.may_submit
    assert [(i.kind, i.key) for i in result.report.items] == [("unresolved_order", CID)]


# ---- the simulated broker ---------------------------------------------------------------------


def sim_fill(state, qty):
    insert_order(state, qty=qty)
    state.execute(
        "UPDATE orders SET status = 'filled', state = 'filled' WHERE client_id = ?", [CID]
    )
    state.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at, portfolio_id)"
        " VALUES (?, 'AAPL.US', ?, 200.0, 0.0, ?, ?)",
        [CID, qty, MON.isoformat(), PF],
    )


def test_the_simulated_broker_reconciles_too(state):
    from stonks.backtest.simulated_broker import SimulatedBroker

    sim_fill(state, 10)
    same = SimulatedBroker(portfolio=Portfolio(cash=1000.0, positions={"AAPL.US": 10.0}))
    assert check(state, same).status == "clean"
    short = SimulatedBroker(portfolio=Portfolio(cash=1000.0, positions={"AAPL.US": 4.0}))
    result = check(state, short)
    assert result.status == "drift"
    assert [(i.kind, i.ours, i.broker) for i in result.report.items] == [
        ("position_qty", 10.0, 4.0)
    ]


def test_reports_list_newest_first(state):
    from stonks.backtest.simulated_broker import SimulatedBroker

    broker = SimulatedBroker(portfolio=Portfolio(cash=0.0, positions={}))
    first = check(state, broker, "sod", now=MON).report
    second = check(state, broker, "eod", now=MON + timedelta(hours=7)).report
    assert [r.id for r in list_reports(state, portfolio_ids=[PF])] == [second.id, first.id]
    assert list_reports(state, portfolio_ids=["pf_other"]) == []
    assert list_reports(state, portfolio_ids=[]) == []
