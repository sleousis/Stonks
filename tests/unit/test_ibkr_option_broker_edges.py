"""Options at IBKR, the edges (roadmap 17.8): a submit whose outcome is
unknown, a duplicate order id, one-leg combos, a gateway without option
data, quote and chain failures, and events that cannot be named."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.combos import ComboLeg, ComboOrder
from stonks.core.options import OptionContract
from stonks.core.types import Order
from stonks.execution.brokers.base import OrderOutcomeUnknownError, OrderRejectedError
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.execution.brokers.ibkr.client import (
    IbApiError,
    IbContractDetails,
    IbContractQuery,
    IbOptionEvent,
    IbOptionParams,
    IbOptionSnapshot,
    IbSnapshot,
    IbWhatIf,
)
from stonks.options.live.gate import options_live_state
from tests.fakes.ib_gateway import AAPL, MSFT, T0, FakeIbGateway, option

EXP = date(2026, 10, 16)
DAY = date(2026, 9, 28)
C200 = option(1001, "AAPL", EXP, "C", 200.0)
C210 = option(1002, "AAPL", EXP, "C", 210.0)
CALL_200 = OptionContract("AAPL.US", EXP, 200.0, "call")
CALL_210 = OptionContract("AAPL.US", EXP, 210.0, "call")
OPEN = options_live_state(True, "live_small", "spreads")


def make(gw: FakeIbGateway | None = None, **kw) -> tuple[IbkrBroker, FakeIbGateway]:
    gw = gw or FakeIbGateway(contracts=(AAPL, MSFT, C200, C210))
    broker = IbkrBroker(gw, mode="paper", options_gate=lambda: OPEN, **kw)
    broker.ensure_ready()
    return broker, gw


def sell_call(client_id: str = "opt-1", **kw) -> Order:
    base = {"client_id": client_id, "ticker": CALL_200.contract_id, "side": "sell",
            "quantity": 1.0, "order_type": "limit", "limit_price": 3.07,
            "position_effect": "open"}  # fmt: skip
    base.update(kw)
    return Order(**base)


def one_leg(net_limit: float | None = -3.10) -> ComboOrder:
    return ComboOrder("cmb-one", (ComboLeg("sell", 1, contract=CALL_200),), net_limit=net_limit)


def _snap(con_id: int, **kw) -> IbOptionSnapshot:
    base = {"con_id": con_id, "bid": 1.0, "ask": 1.2, "last": None, "time": T0, "iv": 0.3,
            "delta": 0.5, "gamma": 0.02, "vega": 0.2, "theta": -0.05,
            "underlying_price": 200.0}  # fmt: skip
    base.update(kw)
    return IbOptionSnapshot(**base)


# ---- submits ---------------------------------------------------------------------------


def test_a_submit_without_an_answer_is_an_unknown_outcome():
    broker, gw = make()
    gw.submit_fault = "timeout_after"
    with pytest.raises(OrderOutcomeUnknownError, match="reconcile before sending"):
        broker.place_order(sell_call())


def test_a_duplicate_order_id_for_an_order_ibkr_has_is_not_an_error():
    broker, gw = make()
    gw.reject_next = (103, "Duplicate order id")
    broker.place_order(sell_call())  # IBKR knows the order: the submit stands
    assert gw.sent_count("opt-1") == 1


def test_a_rejected_option_order_is_a_broker_rejection():
    broker, gw = make()
    gw.reject_next = (201, "Order rejected - reason: no options permission")
    with pytest.raises(OrderRejectedError):
        broker.place_order(sell_call())


def test_a_one_leg_combo_needs_a_net_limit_and_goes_out_once():
    broker, gw = make()
    with pytest.raises(OrderRejectedError, match="net limit"):
        broker.place_combo(one_leg(None))
    broker.place_combo(one_leg())
    broker.place_combo(one_leg())
    assert gw.sent_count("cmb-one:0") == 1


def test_a_one_leg_combo_what_if_previews_the_option_order():
    broker, gw = make()
    gw.what_if_result = IbWhatIf(300.0, 250.0, float("inf"), None, "", "")
    preview = broker.what_if_combo(one_leg())
    contract, request = gw.what_ifs[-1]
    assert contract.sec_type == "OPT" and request.limit_price == pytest.approx(3.10)
    assert preview.initial_margin_change == 300.0
    assert preview.equity_with_loan_after == 0.0  # IBKR's "no value" is never a number
    assert preview.commission is None and preview.warning is None
    with pytest.raises(OrderRejectedError, match="net limit"):
        broker.what_if_combo(one_leg(None))
    assert not gw.sent


def test_an_id_that_is_no_order_and_no_leg_has_no_state():
    broker, _ = make()
    assert broker.get_order_state("never-sent") is None
    assert broker.get_order_state("cmb-none:0") is None


def test_stock_executions_keep_their_own_client_id():
    broker, gw = make()
    buy = Order(client_id="t1-s1-AAPL.US-buy", ticker="AAPL.US", side="buy", quantity=5.0,
                decision_price=200.0)  # fmt: skip
    broker.place_order(buy)
    gw.fill("t1-s1-AAPL.US-buy", 5, 200.5)
    for _ in range(2):  # the second read knows the order is no BAG
        execs = broker.executions(T0.replace(hour=0))
        assert [e.client_id for e in execs] == ["t1-s1-AAPL.US-buy"]


# ---- quotes and chains ------------------------------------------------------------------


class NoOptionData:
    """A gateway client without the option data and event calls."""

    def __init__(self, inner: FakeIbGateway) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        if name.startswith("option_"):
            raise AttributeError(name)
        return getattr(self._inner, name)


def test_a_client_without_option_data_has_no_quotes_and_no_chain():
    gw = FakeIbGateway(contracts=(AAPL, C200))
    broker = IbkrBroker(NoOptionData(gw), mode="paper", options_gate=lambda: OPEN)  # type: ignore[arg-type]
    assert broker.option_quotes([CALL_200.contract_id], DAY) == {}
    assert broker.option_chain("AAPL.US", DAY, max_expiry_days=60, strike_band=0.1) == []
    assert broker.option_events() == []


def test_quotes_skip_unknown_contracts_and_drop_non_numbers():
    broker, gw = make()
    gw.option_snapshot_data[1001] = _snap(1001, delta=float("nan"), iv=0.0, bid=float("inf"))
    unknown = OptionContract("AAPL.US", EXP, 205.0, "call").contract_id
    quotes = broker.option_quotes([unknown, CALL_200.contract_id], DAY)
    q = quotes[CALL_200.contract_id]
    assert list(quotes) == [CALL_200.contract_id]
    assert q.delta is None and q.iv is None and q.bid is None and q.ask == 1.2


@pytest.mark.parametrize(
    "error",
    [IbApiError(321, "Error validating request"), ConnectionError("socket closed")],
)
def test_a_quote_failure_other_than_no_subscription_is_raised(error):
    class Failing(FakeIbGateway):
        def option_snapshots(self, contracts):
            raise error

    broker, _ = make(Failing(contracts=(AAPL, C200)))
    with pytest.raises(Exception) as info:
        broker.option_quotes([CALL_200.contract_id], DAY)
    assert info.value is not error and not isinstance(info.value, IbApiError)


def test_a_chain_lookup_failure_is_raised_as_a_broker_error():
    class Failing(FakeIbGateway):
        def option_params(self, symbol, underlying_con_id):
            raise IbApiError(200, "No security definition has been found")

    broker, _ = make(Failing(contracts=(AAPL, C200)))
    with pytest.raises(Exception) as info:
        broker.option_chain("AAPL.US", DAY, max_expiry_days=60, strike_band=0.1)
    assert not isinstance(info.value, IbApiError)


def test_the_chain_keeps_only_standard_series_of_the_underlying():
    """IBKR answers with an adjusted series, a series of another symbol, a
    contract that is no nameable option and a broken expiry: only the
    standard AAPL series is kept."""
    adjusted = option(1006, "AAPL", EXP, "C", 200.0, multiplier="150")
    other = option(2001, "MSFT", EXP, "C", 200.0)
    nameless = option(3001, "", EXP, "C", 200.0)

    class Messy(FakeIbGateway):
        def option_params(self, symbol, underlying_con_id):
            return [
                IbOptionParams("SMART", "AAPL", "100", ("20261016", "2026XX01"), (200.0,)),
                IbOptionParams("SMART", "AAPL", "150", ("20261016",), (200.0,)),
            ]

        def contract_details(self, query: IbContractQuery) -> list[IbContractDetails]:
            if query.sec_type == "OPT":
                self.lookups.append(query)
                return [C200, adjusted, other, nameless]
            return super().contract_details(query)

    gw = Messy(contracts=(AAPL, C200))
    broker, _ = make(gw)
    gw.snapshot_data[265598] = IbSnapshot(265598, 200.0, 199.9, 200.1, T0)
    for con_id in (1001, 1006, 2001, 3001, 4242):
        gw.option_snapshot_data[con_id] = _snap(con_id)
    gw.option_snapshots = lambda contracts: [_snap(c.con_id) for c in contracts] + [_snap(4242)]  # type: ignore[method-assign]
    rows = broker.option_chain("AAPL.US", DAY, max_expiry_days=60, strike_band=0.1)
    assert [r.contract_id for r in rows] == [CALL_200.contract_id]
    assert [q.last_trade_date for q in gw.lookups if q.sec_type == "OPT"] == ["20261016"]


# ---- events --------------------------------------------------------------------------------


def test_events_from_the_statement_reader_join_and_unnamed_ones_are_dropped():
    gw = FakeIbGateway(contracts=(AAPL, C200))
    from_flex = IbOptionEvent("F1", "DU1234567", C200.contract, "exercise", 1.0, T0)
    stock = IbOptionEvent("F2", "DU1234567", AAPL.contract, "expiry", 1.0, T0)
    broker, _ = make(gw, option_event_reader=lambda: [from_flex, stock])
    gw.option_event(C200, "assignment", -1, event_id="G1")
    events = broker.option_events()
    assert sorted((e.event_id, e.kind) for e in events) == [
        ("F1", "exercise"),
        ("G1", "assignment"),
    ]
