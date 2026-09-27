"""``IbkrBroker`` against ``FakeIbGateway`` (roadmap 19.2)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from stonks.core.types import Order
from stonks.execution.brokers.base import (
    BrokerError,
    BrokerUnavailableError,
    LiveTradingRefusedError,
    OrderOutcomeUnknownError,
    OrderRejectedError,
    broker_capabilities,
)
from stonks.execution.brokers.ibkr.broker import IbkrBroker, account_state
from stonks.execution.brokers.ibkr.client import IbAccountValue, IbSnapshot, IbWhatIf
from tests.fakes.ib_gateway import AAPL, MSFT, T0, FakeIbGateway

ACCOUNT = "DU1234567"


def buy(client_id: str = "t1-s1-AAPL.US-buy", **kw) -> Order:
    base = {"client_id": client_id, "ticker": "AAPL.US", "side": "buy", "quantity": 10.0,
            "decision_price": 200.0}  # fmt: skip
    base.update(kw)
    return Order(**base)


def make(gw: FakeIbGateway | None = None, **kw) -> tuple[IbkrBroker, FakeIbGateway]:
    gw = gw or FakeIbGateway()
    kw.setdefault("mode", "paper")
    return IbkrBroker(gw, **kw), gw


def test_has_every_live_capability():
    broker, _ = make()
    assert broker_capabilities(broker) == {
        "order_state",
        "cancel",
        "global_cancel",
        "account",
        "what_if",
        "executions",
        "quotes",
    }


# ---- account safety check ---------------------------------------------------------------


def test_paper_gateway_with_one_du_account_is_accepted():
    broker, _ = make()
    assert broker.account_id == ACCOUNT


def test_paper_gateway_on_a_live_account_is_refused():
    broker, _ = make(FakeIbGateway(["U7654321"]))
    with pytest.raises(LiveTradingRefusedError, match="live account"):
        broker.ensure_ready()


def test_live_gateway_on_a_paper_account_is_refused():
    broker, _ = make(mode="live", allow_live=True)
    with pytest.raises(LiveTradingRefusedError, match="paper"):
        broker.ensure_ready()


def test_wrong_expected_account_is_refused():
    broker, _ = make(account_id="DU0000001")
    with pytest.raises(LiveTradingRefusedError, match="another account"):
        broker.ensure_ready()


def test_several_accounts_need_an_account_id():
    broker, _ = make(FakeIbGateway(["DU1", "DU2"]))
    with pytest.raises(LiveTradingRefusedError, match="2 accounts"):
        broker.ensure_ready()
    ok, _ = make(FakeIbGateway(["DU1", "DU2"]), account_id="DU2")
    assert ok.account_id == "DU2"


def test_no_managed_account_means_the_login_did_not_finish():
    broker, _ = make(FakeIbGateway([]))
    with pytest.raises(LiveTradingRefusedError, match="no managed account"):
        broker.ensure_ready()


def test_live_orders_need_allow_live():
    gw = FakeIbGateway(["U7654321"])
    broker, _ = make(gw, mode="live", stage_lookup=lambda: "live_small")
    assert broker.account_id == "U7654321"  # reads are fine
    with pytest.raises(LiveTradingRefusedError, match="allow_live"):
        broker.place_order(buy())
    assert gw.sent == []
    allowed, _ = make(gw, mode="live", allow_live=True, stage_lookup=lambda: "live_small")
    allowed.place_order(buy())
    assert gw.sent_count("t1-s1-AAPL.US-buy") == 1


@pytest.mark.parametrize("stage", [None, "sim_paper", "broker_paper"])
def test_live_orders_need_stage_live_small_or_higher(stage):
    gw = FakeIbGateway(["U7654321"])
    broker, _ = make(gw, mode="live", allow_live=True, stage_lookup=lambda: stage)
    with pytest.raises(LiveTradingRefusedError, match="live_small"):
        broker.place_order(buy())
    assert gw.sent == []


def test_live_orders_without_a_stage_lookup_are_refused():
    gw = FakeIbGateway(["U7654321"])
    broker, _ = make(gw, mode="live", allow_live=True)
    with pytest.raises(LiveTradingRefusedError, match="live_small"):
        broker.place_order(buy())


def test_a_failing_stage_lookup_refuses():
    def boom():
        raise RuntimeError("state db locked")

    gw = FakeIbGateway(["U7654321"])
    broker, _ = make(gw, mode="live", allow_live=True, stage_lookup=boom)
    with pytest.raises(LiveTradingRefusedError, match="stage"):
        broker.place_order(buy())
    assert gw.sent == []


def test_a_demoted_live_book_may_still_close_and_cancel():
    gw = FakeIbGateway(["U7654321"])
    broker, _ = make(gw, mode="live", allow_live=True, stage_lookup=lambda: "broker_paper")
    broker.place_order(buy("t1-s1-AAPL.US-sell", side="sell", position_effect="close"))
    assert gw.sent_count("t1-s1-AAPL.US-sell") == 1
    assert broker.cancel_order("t1-s1-AAPL.US-sell") is True


@pytest.mark.parametrize("stage", ["live_small", "live_scale"])
def test_live_stages_may_open(stage):
    gw = FakeIbGateway(["U7654321"])
    broker, _ = make(gw, mode="live", allow_live=True, stage_lookup=lambda: stage)
    broker.place_order(buy())
    assert gw.sent_count("t1-s1-AAPL.US-buy") == 1


def test_paper_gateways_ignore_the_stage():
    broker, gw = make(stage_lookup=lambda: "sim_paper")
    broker.place_order(buy())
    assert gw.sent_count("t1-s1-AAPL.US-buy") == 1


def test_account_is_checked_again_after_a_reconnect():
    broker, gw = make()
    broker.ensure_ready()
    gw.drop()
    gw.accounts = ["U7654321"]
    with pytest.raises(LiveTradingRefusedError):
        broker.ensure_ready()


def test_competing_session_and_lost_link_refuse():
    broker, gw = make()
    gw.competing = True
    with pytest.raises(BrokerUnavailableError, match="competing"):
        broker.ensure_ready()
    gw.competing = False
    gw.link_ok = False
    with pytest.raises(BrokerUnavailableError, match="1100"):
        broker.place_order(buy())
    assert gw.sent == []


def test_gateway_down_is_unavailable():
    gw = FakeIbGateway()
    gw.connect_failures = 1
    broker, _ = make(gw)
    with pytest.raises(BrokerUnavailableError):
        broker.ensure_ready()
    broker.ensure_ready()


# ---- login check (the health probe) -------------------------------------------------------


def test_login_check_ok():
    broker, _ = make()
    check = broker.login_check()
    assert check.ok and check.fault is None
    assert check.account_id == ACCOUNT and check.server_time == T0


def test_login_check_faults():
    wrong, _ = make(FakeIbGateway(["U1"]))
    assert wrong.login_check().fault == "wrong_account"
    assert not wrong.login_check().ok
    lost, lost_gw = make()
    lost_gw.link_ok = False
    assert not lost.login_check().ok and lost.login_check().fault is None
    refused, _ = make(FakeIbGateway([]))
    assert refused.login_check().fault == "login_refused"
    competing, gw = make()
    gw.competing = True
    check = competing.login_check()
    assert check.fault == "competing_session" and not check.ok
    down_gw = FakeIbGateway()
    down_gw.connect_failures = 5
    down, _ = make(down_gw)
    check = down.login_check()
    assert not check.connected and check.fault is None


def test_login_check_reports_other_broker_errors():
    class Broken(FakeIbGateway):
        def server_time(self):
            from stonks.execution.brokers.ibkr.client import IbApiError

            raise IbApiError(321, "bad")

    broker, _ = make(Broken())
    check = broker.login_check()
    assert not check.connected and "321" in check.detail


# ---- orders -----------------------------------------------------------------------------


def test_place_order_sends_a_collared_opening_limit():
    broker, gw = make()
    assert broker.place_order(buy()) is None
    contract, req = gw.sent[0]
    assert contract.con_id == AAPL.contract.con_id
    assert (req.order_type, req.tif, req.limit_price, req.account) == (
        "LMT",
        "OPG",
        202.0,
        ACCOUNT,
    )
    state = broker.get_order_state("t1-s1-AAPL.US-buy")
    assert state is not None
    assert state.state == "accepted" and state.status == "pending"
    assert state.broker_order_id == str(gw.trade(req.order_ref).perm_id)
    assert state.ticker == "AAPL.US"


def test_place_order_never_sends_twice():
    broker, gw = make()
    broker.place_order(buy())
    broker.place_order(buy())
    fresh, _ = make(gw)  # a new process
    fresh.place_order(buy())
    assert gw.sent_count("t1-s1-AAPL.US-buy") == 1


def test_a_completed_or_executed_order_is_not_sent_again():
    broker, gw = make()
    broker.place_order(buy())
    gw.fill("t1-s1-AAPL.US-buy", 10, 201.0)
    broker.place_order(buy())
    assert gw.sent_count("t1-s1-AAPL.US-buy") == 1


def test_disconnect_mid_submit_is_outcome_unknown_and_then_found():
    broker, gw = make()
    gw.submit_fault = "disconnect_after"
    with pytest.raises(OrderOutcomeUnknownError) as info:
        broker.place_order(buy())
    assert info.value.client_id == "t1-s1-AAPL.US-buy"
    # a retry after the reconnect finds the order and does not resend
    broker.place_order(buy())
    assert gw.sent_count("t1-s1-AAPL.US-buy") == 1


def test_timeout_mid_submit_is_outcome_unknown():
    broker, gw = make()
    gw.submit_fault = "timeout_after"
    with pytest.raises(OrderOutcomeUnknownError):
        broker.place_order(buy())


def test_disconnect_before_submit_resends_safely_after_reconnect():
    broker, gw = make()
    broker.ensure_ready()
    gw.submit_fault = "disconnect_before"
    with pytest.raises(OrderOutcomeUnknownError):
        broker.place_order(buy())
    broker.place_order(buy())
    assert gw.sent_count("t1-s1-AAPL.US-buy") == 1


def test_rejection_carries_ibkr_text():
    broker, gw = make()
    gw.reject_next = (201, "Order rejected - reason: insufficient funds")
    with pytest.raises(OrderRejectedError, match="insufficient funds"):
        broker.place_order(buy())
    state = broker.get_order_state("t1-s1-AAPL.US-buy")
    assert state is not None and state.state == "unknown"


def test_duplicate_order_id_rechecks_by_order_ref():
    class Dup(FakeIbGateway):
        def place_order(self, contract, order):
            super().place_order(contract, order)
            from stonks.execution.brokers.ibkr.client import IbApiError

            raise IbApiError(103, "Duplicate order id")

    broker, gw = make(Dup())
    broker.place_order(buy())  # found by orderRef: no error


def test_duplicate_order_id_with_nothing_found_is_an_error():
    class Dup(FakeIbGateway):
        def place_order(self, contract, order):
            from stonks.execution.brokers.ibkr.client import IbApiError

            raise IbApiError(103, "Duplicate order id")

    broker, _ = make(Dup())
    with pytest.raises(BrokerError, match="103"):
        broker.place_order(buy())


def test_short_sales_are_refused_on_a_long_only_account():
    broker, gw = make()
    with pytest.raises(OrderRejectedError, match="long only"):
        broker.place_order(buy(side="sell", position_effect="open", client_id="s"))
    assert gw.sent == []


def test_unknown_ticker_is_refused_before_sending():
    broker, gw = make()
    with pytest.raises(Exception, match="no IBKR contract"):
        broker.place_order(buy(ticker="ZZZZ.US"))
    assert gw.sent == []


def test_hashed_order_ref_round_trips():
    long_id = "tick-" + "x" * 60
    broker, gw = make()
    broker.place_order(buy(client_id=long_id))
    ref = broker.broker_ref(long_id)
    assert ref != long_id and gw.sent_count(ref) == 1
    gw.fill(ref, 10, 201.0, commission=1.0)
    (execution,) = broker.executions(T0 - timedelta(hours=1))
    assert execution.client_id == long_id
    # a fresh process maps it back through the stored broker_ref
    fresh, _ = make(gw, ref_lookup={ref: long_id}.get)
    assert fresh.executions(T0 - timedelta(hours=1))[0].client_id == long_id
    blind, _ = make(gw)
    assert blind.executions(T0 - timedelta(hours=1))[0].client_id == ref


# ---- order states -----------------------------------------------------------------------


def test_partial_fill_then_filled():
    broker, gw = make()
    broker.place_order(buy())
    gw.fill("t1-s1-AAPL.US-buy", 4, 200.0)
    s = broker.get_order_state("t1-s1-AAPL.US-buy")
    assert s.state == "partially_filled" and s.filled_quantity == 4
    gw.fill("t1-s1-AAPL.US-buy", 6, 201.0)
    s = broker.get_order_state("t1-s1-AAPL.US-buy")
    assert s.state == "filled" and s.status == "filled"
    assert s.avg_fill_price == pytest.approx(200.6)


def test_auction_no_fill_is_expired():
    broker, gw = make()
    broker.place_order(buy())
    gw.auction_no_fill("t1-s1-AAPL.US-buy")
    s = broker.get_order_state("t1-s1-AAPL.US-buy")
    assert s.state == "expired" and s.status == "cancelled"


def test_unknown_client_id_has_no_state():
    broker, _ = make()
    assert broker.get_order_state("never-sent") is None


def test_executions_without_an_order_record_are_unknown():
    broker, gw = make()
    broker.place_order(buy())
    gw.fill("t1-s1-AAPL.US-buy", 10, 201.0)
    # the gateway forgot the order but still lists the execution
    gw._trades.clear()
    s = broker.get_order_state("t1-s1-AAPL.US-buy")
    assert s.state == "unknown" and s.filled_quantity == 10
    assert s.avg_fill_price == pytest.approx(201.0)


def test_gateway_restart_keeps_orders_findable():
    broker, gw = make()
    broker.place_order(buy())
    gw.restart()
    assert broker.get_order_state("t1-s1-AAPL.US-buy").state == "accepted"
    assert broker.cancel_order("t1-s1-AAPL.US-buy") is True
    assert gw.cancels == [1000]


# ---- cancels ----------------------------------------------------------------------------


def test_cancel_order():
    broker, gw = make()
    broker.place_order(buy(order_type="limit", limit_price=199.0, time_in_force="day"))
    assert broker.cancel_order("t1-s1-AAPL.US-buy") is True
    assert broker.get_order_state("t1-s1-AAPL.US-buy").state == "cancelled"
    assert broker.cancel_order("t1-s1-AAPL.US-buy") is False
    assert broker.cancel_order("never-sent") is False


def test_pending_cancel_stays_working():
    broker, gw = make()
    broker.place_order(buy())
    gw.cancel_leaves_pending = True
    assert broker.cancel_order("t1-s1-AAPL.US-buy") is True
    assert broker.get_order_state("t1-s1-AAPL.US-buy").state == "pending_cancel"


def test_cancel_that_loses_the_race_returns_false():
    class Race(FakeIbGateway):
        def cancel_order(self, order_id):
            from stonks.execution.brokers.ibkr.client import IbApiError

            raise IbApiError(161, "not open")

    broker, _ = make(Race())
    broker.place_order(buy())
    assert broker.cancel_order("t1-s1-AAPL.US-buy") is False


def test_cancel_errors_map():
    class Broken(FakeIbGateway):
        def cancel_order(self, order_id):
            raise TimeoutError()

    broker, _ = make(Broken())
    broker.place_order(buy())
    with pytest.raises(BrokerUnavailableError):
        broker.cancel_order("t1-s1-AAPL.US-buy")

    class Rejected(FakeIbGateway):
        def cancel_order(self, order_id):
            from stonks.execution.brokers.ibkr.client import IbApiError

            raise IbApiError(10147, "odd")

    broker, _ = make(Rejected())
    broker.place_order(buy())
    with pytest.raises(BrokerError, match="10147"):
        broker.cancel_order("t1-s1-AAPL.US-buy")


def test_cancel_all_includes_manual_orders():
    broker, gw = make()
    broker.place_order(buy())
    gw.add_manual_order(MSFT, 5)
    assert broker.cancel_all() == 2
    assert gw.global_cancels == 1
    assert gw.open_trades() == []


# ---- executions and commissions ----------------------------------------------------------


def test_executions_with_late_commissions():
    broker, gw = make()
    broker.place_order(buy())
    exec_id = gw.fill("t1-s1-AAPL.US-buy", 10, 201.0)
    (e,) = broker.executions(T0 - timedelta(hours=1))
    assert (e.client_id, e.ticker, e.side, e.quantity, e.price) == (
        "t1-s1-AAPL.US-buy",
        "AAPL.US",
        "buy",
        10,
        201.0,
    )
    assert e.commission is None
    gw.report_commission(exec_id, 1.25, "USD")
    (e,) = broker.executions(T0 - timedelta(hours=1))
    assert e.commission == 1.25 and e.commission_currency == "USD"
    assert broker.executions(T0 + timedelta(minutes=1)) == []


def test_manual_and_other_account_executions_are_not_ours():
    broker, gw = make()
    manual = gw.add_manual_order(MSFT, 5)
    gw.fill(manual.order_ref, 5, 400.0)  # orderRef "" : placed by hand
    broker.place_order(buy())
    gw.fill("t1-s1-AAPL.US-buy", 10, 201.0)
    other = replace(gw._executions[-1], account="DU999", exec_id="other")
    gw._executions.append(other)
    assert [e.broker_exec_id for e in broker.executions(T0 - timedelta(days=1))] == ["0001.0002"]


def test_reconcile_returns_each_execution_once():
    broker, gw = make()
    broker.place_order(buy())
    gw.fill("t1-s1-AAPL.US-buy", 4, 200.0, commission=0.5)
    (fill,) = broker.reconcile()
    assert fill.quantity == 4 and fill.fee == 0.5 and fill.broker_exec_id
    assert broker.reconcile() == []
    gw.fill("t1-s1-AAPL.US-buy", 6, 200.0)
    assert len(broker.reconcile()) == 1


# ---- positions and account ---------------------------------------------------------------


def test_fetch_portfolio_maps_positions_by_con_id():
    broker, gw = make()
    broker.ensure_ready()
    broker.resolver.resolve("AAPL.US")
    gw.set_position(AAPL, 10)
    gw.set_position(MSFT, 3)  # never resolved here: raw symbol
    gw.set_position(AAPL, 0)
    gw.set_values(NetLiquidation="100000", TotalCashValue="50000")
    p = broker.fetch_portfolio()
    assert p.positions == {"AAPL.US": 10, "MSFT": 3}
    assert p.cash == 50000


def test_fetch_account_maps_the_tags():
    broker, gw = make()
    gw.set_values(
        NetLiquidation=("120000", "EUR"),
        TotalCashValue=("20000", "EUR"),
        SettledCash=("15000", "EUR"),
        AvailableFunds=("18000", "EUR"),
        BuyingPower=("18000", "EUR"),
        ExcessLiquidity=("17000", "EUR"),
        DayTradesRemaining=("-1", "EUR"),
        FullInitMarginReq=("0", "EUR"),
        CashBalance=("5000", "USD"),
    )
    gw.values_by_account[ACCOUNT].append(IbAccountValue(ACCOUNT, "CashBalance", "20000", "BASE"))
    gw.values_by_account[ACCOUNT].append(IbAccountValue(ACCOUNT, "AccountType", "INDIVIDUAL", ""))
    a = broker.fetch_account()
    assert (a.equity, a.cash, a.settled_cash, a.available_funds, a.currency) == (
        120000,
        20000,
        15000,
        18000,
        "EUR",
    )
    assert a.day_trades_remaining is None
    assert a.excess_liquidity == 17000
    assert a.cash_by_currency == {"USD": 5000}
    assert a.account_type == "cash" and a.account_id == ACCOUNT


def test_account_state_needs_net_liquidation_and_skips_other_accounts():
    with pytest.raises(BrokerError, match="NetLiquidation"):
        account_state([], account_id="DU1", account_type="cash")
    values = [
        IbAccountValue("DU2", "NetLiquidation", "5", "USD"),
        IbAccountValue("DU1", "NetLiquidation", "7", "USD"),
        IbAccountValue("DU1", "DayTradesRemaining", "3", "USD"),
        IbAccountValue("DU1", "BuyingPower", "n/a", "USD"),
    ]
    a = account_state(values, account_id="DU1", account_type="margin")
    assert a.equity == 7 and a.day_trades_remaining == 3 and a.buying_power == 0
    assert a.settled_cash == 0.0


# ---- what-if ----------------------------------------------------------------------------


def test_what_if_maps_the_preview():
    broker, gw = make()
    gw.what_if_result = IbWhatIf(
        init_margin_change=500.0,
        maint_margin_change=400.0,
        equity_with_loan_after=99_000.0,
        commission=1.7976931348623157e308,  # IBKR's "not set"
        commission_currency="",
        warning="",
    )
    p = broker.what_if(buy())
    assert (p.initial_margin_change, p.maintenance_margin_change) == (500.0, 400.0)
    assert p.commission is None and p.commission_currency is None and p.warning is None
    assert gw.sent == []


def test_what_if_timeout_raises():
    broker, gw = make()
    gw.what_if_timeout = True
    with pytest.raises(BrokerUnavailableError):
        broker.what_if(buy())


def test_what_if_unset_values_are_zero():
    broker, gw = make()
    gw.what_if_result = IbWhatIf(None, float("nan"), None, 2.0, "USD", "margin warning")
    p = broker.what_if(buy())
    assert (p.initial_margin_change, p.maintenance_margin_change) == (0.0, 0.0)
    assert p.commission == 2.0 and p.warning == "margin warning"


# ---- quotes -----------------------------------------------------------------------------


def test_quotes_live_and_delayed():
    broker, gw = make()
    gw.snapshot_data = {
        AAPL.contract.con_id: IbSnapshot(AAPL.contract.con_id, 201.0, 200.9, 201.1, T0, 1),
        MSFT.contract.con_id: IbSnapshot(MSFT.contract.con_id, float("nan"), 400.0, 400.2, T0, 3),
    }
    q = broker.quotes(["AAPL.US", "MSFT.US", "ZZZZ.US"])
    assert set(q) == {"AAPL.US", "MSFT.US"}
    assert not q["AAPL.US"].delayed and q["AAPL.US"].last == 201.0
    assert q["MSFT.US"].delayed and q["MSFT.US"].last is None
    assert q["MSFT.US"].mid == pytest.approx(400.1)


def test_quotes_without_data_are_left_out():
    broker, gw = make()
    gw.snapshot_data = {AAPL.contract.con_id: IbSnapshot(AAPL.contract.con_id, -1, -1, -1, T0)}
    assert broker.quotes(["AAPL.US", "MSFT.US"]) == {}
    assert broker.quotes(["ZZZZ.US"]) == {}
    gw.no_market_data = True
    assert broker.quotes(["AAPL.US"]) == {}


def test_quote_errors_map():
    class Broken(FakeIbGateway):
        def snapshots(self, contracts):
            raise TimeoutError()

    broker, _ = make(Broken())
    with pytest.raises(BrokerUnavailableError):
        broker.quotes(["AAPL.US"])

    class Odd(FakeIbGateway):
        def snapshots(self, contracts):
            from stonks.execution.brokers.ibkr.client import IbApiError

            raise IbApiError(321, "odd")

    broker, _ = make(Odd())
    with pytest.raises(BrokerError, match="321"):
        broker.quotes(["AAPL.US"])
