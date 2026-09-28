"""Options at IBKR through ``FakeIbGateway`` (roadmap 17.8)."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.combos import ComboLeg, ComboOrder
from stonks.core.options import OptionContract
from stonks.core.types import Order
from stonks.execution.brokers.base import (
    LiveTradingRefusedError,
    OrderRejectedError,
    UnsupportedTickerError,
)
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.execution.brokers.ibkr.client import IbOptionSnapshot, IbSnapshot, IbWhatIf
from stonks.execution.brokers.ibkr.options import option_id_for_contract, snap_net
from stonks.options.live.broker import OptionBroker
from stonks.options.live.gate import options_live_state
from tests.fakes.ib_gateway import AAPL, MSFT, T0, FakeIbGateway, option

EXP = date(2026, 10, 16)
NEXT = date(2026, 11, 20)
C200 = option(1001, "AAPL", EXP, "C", 200.0)
C210 = option(1002, "AAPL", EXP, "C", 210.0)
P190 = option(1003, "AAPL", EXP, "P", 190.0)
C200_NOV = option(1004, "AAPL", NEXT, "C", 200.0)
FAR = option(1005, "AAPL", date(2027, 6, 18), "C", 200.0)
ADJ = option(1006, "AAPL", EXP, "C", 200.0, multiplier="150")

CALL_200 = OptionContract("AAPL.US", EXP, 200.0, "call")
CALL_210 = OptionContract("AAPL.US", EXP, 210.0, "call")
PUT_190 = OptionContract("AAPL.US", EXP, 190.0, "put")
OPEN = options_live_state(True, "live_small", "spreads")
CLOSED = options_live_state(False, "live_small", "spreads")


def gateway() -> FakeIbGateway:
    return FakeIbGateway(contracts=(AAPL, MSFT, C200, C210, P190, C200_NOV, FAR, ADJ))


def make(gw: FakeIbGateway | None = None, *, gate=OPEN, **kw) -> tuple[IbkrBroker, FakeIbGateway]:
    gw = gw or gateway()
    kw.setdefault("mode", "paper")
    broker = IbkrBroker(gw, options_gate=(lambda: gate) if gate is not None else None, **kw)
    broker.ensure_ready()
    return broker, gw


def sell_call(client_id: str = "cmb-1:0", **kw) -> Order:
    base = {"client_id": client_id, "ticker": CALL_200.contract_id, "side": "sell",
            "quantity": 1.0, "order_type": "limit", "limit_price": 3.07,
            "position_effect": "open"}  # fmt: skip
    base.update(kw)
    return Order(**base)


def spread(client_id: str = "cmb-sprd", **kw) -> ComboOrder:
    base = {
        "client_id": client_id,
        "legs": (ComboLeg("buy", 1, contract=CALL_200), ComboLeg("sell", 1, contract=CALL_210)),
        "quantity": 2.0,
        "structure": "vertical_spread",
        "net_limit": 2.456,
    }
    base.update(kw)
    return ComboOrder(**base)


# ---- contracts -------------------------------------------------------------------------


def test_an_option_id_resolves_to_its_conid_and_is_cached():
    broker, gw = make()
    resolved = broker.resolver.resolve(CALL_200.contract_id)
    assert resolved.con_id == 1001
    spec = resolved.spec()
    assert spec.is_option and spec.multiplier == 100 and spec.broker_id("ibkr") == "1001"
    broker.resolver.resolve(CALL_200.contract_id)
    assert len(gw.lookups) == 1
    query = gw.lookups[0]
    assert (query.sec_type, query.last_trade_date, query.right, query.strike) == (
        "OPT",
        "20261016",
        "C",
        200.0,
    )


def test_an_adjusted_contract_needs_its_own_multiplier():
    broker, _ = make()
    adjusted = OptionContract("AAPL.US", EXP, 200.0, "call", multiplier=150.0)
    assert broker.resolver.resolve(adjusted.contract_id).con_id == 1006
    assert broker.resolver.resolve(CALL_200.contract_id).con_id == 1001


def test_unknown_and_foreign_options_are_refused():
    broker, _ = make()
    with pytest.raises(UnsupportedTickerError):
        broker.resolver.resolve(OptionContract("AAPL.US", EXP, 205.0, "call").contract_id)
    with pytest.raises(UnsupportedTickerError, match="LSE"):
        broker.resolver.resolve(OptionContract("VOD.LSE", EXP, 1.0, "call").contract_id)


def test_option_positions_map_back_to_contract_ids():
    assert option_id_for_contract(C200.contract) == CALL_200.contract_id
    broker, gw = make()
    gw.set_position(P190, -2)
    gw.set_values(NetLiquidation="1000", TotalCashValue="1000")
    assert broker.fetch_portfolio().positions == {PUT_190.contract_id: -2}


# ---- single-leg orders --------------------------------------------------------------------


def test_an_opening_option_order_is_a_day_limit_on_the_tick_grid():
    broker, gw = make()
    broker.place_order(sell_call(limit_price=3.074))
    contract, request = gw.sent[-1]
    assert contract.con_id == 1001
    assert (request.order_type, request.tif, request.action) == ("LMT", "DAY", "SELL")
    assert request.limit_price == pytest.approx(3.08)  # a sell never rounds down
    assert request.total_quantity == 1.0


def test_option_orders_are_never_market_orders():
    broker, gw = make()
    with pytest.raises(OrderRejectedError, match="never market"):
        broker.place_order(sell_call(order_type="market", limit_price=None))
    with pytest.raises(OrderRejectedError, match="whole number"):
        broker.place_order(sell_call(quantity=1.5))
    with pytest.raises(OrderRejectedError, match="day orders"):
        broker.place_order(sell_call(time_in_force="opg"))
    assert not gw.sent


@pytest.mark.parametrize("gate", [None, CLOSED])
def test_a_closed_or_missing_gate_refuses_opening_options(gate):
    broker, gw = make(gate=gate)
    with pytest.raises(LiveTradingRefusedError, match="options"):
        broker.place_order(sell_call())
    assert not gw.sent


def test_a_close_of_a_held_option_passes_a_closed_gate():
    broker, gw = make(gate=None)
    gw.set_position(C200, -1)
    broker.place_order(sell_call(side="buy", position_effect="close"))
    assert gw.sent[-1][1].action == "BUY"


def test_a_close_that_holds_nothing_counts_as_an_open():
    broker, gw = make(gate=CLOSED)
    with pytest.raises(LiveTradingRefusedError):
        broker.place_order(sell_call(side="buy", position_effect="close"))
    assert not gw.sent


def test_an_option_order_is_sent_once():
    broker, gw = make()
    broker.place_order(sell_call())
    broker.place_order(sell_call())
    assert gw.sent_count("cmb-1:0") == 1


def test_a_live_gateway_needs_no_stock_stage_lookup_for_an_option_close():
    gw = FakeIbGateway(["U7654321"], contracts=(AAPL, C200))
    broker, _ = make(gw, gate=None, mode="live", allow_live=True)
    gw.set_position(C200, -1)
    broker.place_order(sell_call(side="buy", position_effect="close"))
    assert gw.sent


# ---- combos --------------------------------------------------------------------------------


def test_a_spread_goes_out_as_one_bag_at_its_net_limit():
    broker, gw = make()
    broker.place_combo(spread())
    contract, request = gw.sent[-1]
    assert contract.sec_type == "BAG" and contract.symbol == "AAPL"
    assert [(leg.con_id, leg.ratio, leg.action) for leg in contract.combo_legs] == [
        (1001, 1, "BUY"),
        (1002, 1, "SELL"),
    ]
    assert (request.action, request.order_type, request.tif) == ("BUY", "LMT", "DAY")
    assert request.limit_price == pytest.approx(2.45)
    assert request.total_quantity == 2.0 and request.order_ref == "cmb-sprd"


def test_a_credit_combo_rounds_to_the_bigger_credit():
    assert snap_net(-1.203, 0.01) == pytest.approx(-1.21)
    assert snap_net(2.456, 0.05) == pytest.approx(2.45)


def test_a_combo_without_a_net_limit_is_refused():
    broker, gw = make()
    with pytest.raises(OrderRejectedError, match="net limit"):
        broker.place_combo(spread(net_limit=None))
    assert not gw.sent


def test_a_combo_is_refused_while_the_gate_is_closed():
    broker, gw = make(gate=CLOSED)
    with pytest.raises(LiveTradingRefusedError):
        broker.place_combo(spread())
    assert not gw.sent


def test_a_combo_is_sent_once():
    broker, gw = make()
    broker.place_combo(spread())
    broker.place_combo(spread())
    assert gw.sent_count("cmb-sprd") == 1


def test_leg_fills_book_on_the_legs_own_client_ids():
    broker, gw = make()
    broker.place_combo(spread())
    gw.fill_combo("cmb-sprd", 2, [4.10, 1.60], commission=1.3)
    execs = broker.executions(T0.replace(hour=0))
    assert sorted((e.client_id, e.side, e.quantity, e.price) for e in execs) == [
        ("cmb-sprd:0", "buy", 2.0, 4.10),
        ("cmb-sprd:1", "sell", 2.0, 1.60),
    ]
    assert {e.ticker for e in execs} == {CALL_200.contract_id, CALL_210.contract_id}
    leg = broker.get_order_state("cmb-sprd:1")
    assert leg is not None
    assert (leg.side, leg.quantity, leg.filled_quantity, leg.status) == ("sell", 2.0, 2.0, "filled")


def test_a_fresh_broker_still_maps_legs_from_the_bag_order():
    first, gw = make()
    first.place_combo(spread())
    gw.fill_combo("cmb-sprd", 1, [4.10, 1.60])
    fresh, _ = make(gw)
    ids = sorted(e.client_id for e in fresh.executions(T0.replace(hour=0)))
    assert ids == ["cmb-sprd:0", "cmb-sprd:1"]
    state = fresh.get_order_state("cmb-sprd:0")
    assert state is not None and state.filled_quantity == 1.0 and state.status != "filled"


def test_a_one_leg_combo_goes_out_as_a_plain_option_order():
    broker, gw = make()
    combo = ComboOrder("cmb-one", (ComboLeg("sell", 1, contract=CALL_200),), net_limit=-3.10)
    broker.place_combo(combo)
    contract, request = gw.sent[-1]
    assert contract.sec_type == "OPT" and request.order_ref == "cmb-one:0"
    assert (request.action, request.limit_price) == ("SELL", 3.10)


def test_the_combo_what_if_is_one_bag_preview():
    broker, gw = make()
    gw.what_if_result = IbWhatIf(500.0, 450.0, 99_000.0, 2.6, "USD")
    preview = broker.what_if_combo(spread())
    assert preview.initial_margin_change == 500.0 and preview.commission == 2.6
    assert gw.what_ifs[-1][0].sec_type == "BAG" and not gw.sent


def test_a_single_option_what_if_uses_the_option_order():
    broker, gw = make()
    broker.what_if(sell_call())
    assert gw.what_ifs[-1][1].order_type == "LMT" and not gw.sent


# ---- quotes, chains and events ---------------------------------------------------------------


def _snap(con_id: int, bid: float, ask: float, delta: float) -> IbOptionSnapshot:
    return IbOptionSnapshot(
        con_id=con_id, bid=bid, ask=ask, last=None, time=T0, iv=0.31, delta=delta,
        gamma=0.02, vega=0.25, theta=-0.05, underlying_price=203.0, open_interest=900,
    )  # fmt: skip


def test_option_quotes_carry_ibkrs_greeks():
    broker, gw = make()
    gw.option_snapshot_data[1001] = _snap(1001, 5.0, 5.2, 0.55)
    quotes = broker.option_quotes([CALL_200.contract_id, CALL_210.contract_id], date(2026, 9, 28))
    assert list(quotes) == [CALL_200.contract_id]
    q = quotes[CALL_200.contract_id]
    assert (q.mid, q.delta, q.iv, q.underlying_price) == (pytest.approx(5.1), 0.55, 0.31, 203.0)


def test_option_quotes_without_the_data_add_on_are_empty():
    broker, gw = make()
    gw.no_market_data = True
    assert broker.option_quotes([CALL_200.contract_id], date(2026, 9, 28)) == {}


def test_the_chain_reads_one_lookup_per_expiry_within_the_horizon_and_band():
    broker, gw = make()
    gw.snapshot_data[265598] = IbSnapshot(265598, 200.0, 199.9, 200.1, T0)
    for d in (C200, C210, P190, C200_NOV, FAR):
        gw.option_snapshot_data[d.contract.con_id] = _snap(d.contract.con_id, 1.0, 1.2, 0.5)
    rows = broker.option_chain("AAPL.US", date(2026, 9, 28), max_expiry_days=60, strike_band=0.04)
    ids = sorted(r.contract_id for r in rows)
    assert ids == sorted(
        [CALL_200.contract_id, OptionContract("AAPL.US", NEXT, 200.0, "call").contract_id]
    )  # 190 and 210 are 5% from spot, June 2027 is beyond 60 days, 150 is adjusted
    assert all(r.delta == 0.5 and r.as_of == date(2026, 9, 28) for r in rows)
    option_lookups = [q for q in gw.lookups if q.sec_type == "OPT"]
    assert len(option_lookups) == 2


def test_assignments_become_option_events():
    broker, gw = make()
    gw.option_event(P190, "assignment", -2, event_id="E1")
    gw.option_event(C200, "expiry", -1, event_id="E2", account="DU0000009")
    events = broker.option_events()
    assert [(e.event_id, e.kind, e.contract_id, e.quantity) for e in events] == [
        ("E1", "assignment", PUT_190.contract_id, -2.0)
    ]


def test_the_broker_offers_the_option_capability():
    broker, _ = make()
    assert isinstance(broker, OptionBroker)


def test_cancelling_a_combo_leg_cancels_its_bag_order():
    """The ledger holds a combo as leg orders ``<combo>:<i>``, the broker as
    one BAG order under the combo id. The kill switch cancels ledger rows,
    so a leg's cancel must reach the BAG."""
    broker, gw = make()
    broker.place_combo(spread())
    assert broker.cancel_order("cmb-sprd:0") is True
    assert gw.trade("cmb-sprd").status == "Cancelled"
    state = broker.get_order_state("cmb-sprd:1")
    assert state is not None and state.state == "cancelled"
    # nothing left to cancel
    assert broker.cancel_order("cmb-sprd:1") is False


def test_a_working_combo_lists_as_its_leg_orders():
    """Reconciliation compares open orders with the ledger, which holds a
    combo as its legs. A BAG order must list as those legs, or a working
    combo reads as an unknown order plus missing legs (broker drift)."""
    from stonks.execution.drift import LedgerOrder, order_drift

    broker, _ = make()
    broker.place_combo(spread())
    working = broker.open_orders()
    assert [(o.client_id, o.ticker, o.side, o.quantity) for o in working] == [
        ("cmb-sprd:0", CALL_200.contract_id, "buy", 2.0),
        ("cmb-sprd:1", CALL_210.contract_id, "sell", 2.0),
    ]
    ledger = [
        LedgerOrder("cmb-sprd:0", CALL_200.contract_id, "accepted"),
        LedgerOrder("cmb-sprd:1", CALL_210.contract_id, "accepted"),
    ]
    items, external = order_drift(ledger, working, allow_manual=True)
    assert items == [] and external == []
