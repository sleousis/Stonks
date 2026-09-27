"""Drift between the broker and the ledger (roadmap 19.5): pure diffs."""

from __future__ import annotations

from stonks.execution.brokers.base import BrokerOpenOrder
from stonks.execution.drift import (
    DriftItem,
    LedgerOrder,
    eod_items,
    order_drift,
    position_drift,
    report_status,
)


def kinds(items):
    return sorted((i.kind, i.key) for i in items)


# ---- positions --------------------------------------------------------------------------


def test_matching_positions_are_clean():
    items, external = position_drift({"AAPL.US": 10}, {"AAPL.US": 10}, allow_manual=True)
    assert items == [] and external == {}


def test_the_owners_extra_shares_are_external_not_drift():
    items, external = position_drift(
        {"AAPL.US": 10}, {"AAPL.US": 25, "MSFT.US": 3}, allow_manual=True
    )
    assert items == []
    assert external == {"AAPL.US": 15, "MSFT.US": 3}


def test_fewer_shares_than_stonks_owns_is_material_drift():
    items, _ = position_drift({"AAPL.US": 10}, {"AAPL.US": 9}, allow_manual=True)
    [item] = items
    assert (item.kind, item.key, item.ours, item.broker) == ("position_qty", "AAPL.US", 10, 9)
    assert item.material and not item.explained


def test_an_owned_position_gone_at_the_broker_is_drift():
    items, _ = position_drift({"AAPL.US": 10}, {}, allow_manual=True)
    assert kinds(items) == [("position_qty", "AAPL.US")]
    assert items[0].broker == 0


def test_an_opposite_sign_holding_does_not_cover_what_stonks_owns():
    items, external = position_drift({"AAPL.US": 10}, {"AAPL.US": -5}, allow_manual=True)
    assert kinds(items) == [("position_qty", "AAPL.US")]
    assert external == {"AAPL.US": -5}


def test_shorts_compare_by_size():
    items, external = position_drift({"AAPL.US": -10}, {"AAPL.US": -12}, allow_manual=True)
    assert items == [] and external == {"AAPL.US": -2}
    items, _ = position_drift({"AAPL.US": -10}, {"AAPL.US": -8}, allow_manual=True)
    assert kinds(items) == [("position_qty", "AAPL.US")]


def test_without_manual_trades_positions_are_exact():
    items, external = position_drift(
        {"AAPL.US": 10}, {"AAPL.US": 11, "MSFT.US": 3}, allow_manual=False
    )
    assert kinds(items) == [("position_qty", "AAPL.US"), ("unknown_position", "MSFT.US")]
    assert all(i.material for i in items)
    assert external == {}


def test_tiny_float_noise_is_not_drift():
    items, external = position_drift(
        {"AAPL.US": 10.0}, {"AAPL.US": 10.0 + 1e-12}, allow_manual=False
    )
    assert items == [] and external == {}


# ---- orders -----------------------------------------------------------------------------


def open_at_broker(client_id, ticker="AAPL.US", broker_id="1"):
    return BrokerOpenOrder(
        broker_order_id=broker_id, client_id=client_id, ticker=ticker, side="buy", quantity=10
    )


def ledger(client_id, state="accepted", ticker="AAPL.US"):
    return LedgerOrder(client_id=client_id, ticker=ticker, state=state)


def test_orders_that_match_are_clean():
    items, external = order_drift([ledger("c1")], [open_at_broker("c1")], allow_manual=True)
    assert items == [] and external == []


def test_a_hand_placed_order_is_external_when_manual_trades_are_allowed():
    items, external = order_drift([], [open_at_broker(None, broker_id="77")], allow_manual=True)
    assert items == []
    assert [o.broker_order_id for o in external] == ["77"]


def test_a_hand_placed_order_is_drift_when_manual_trades_are_off():
    items, _ = order_drift([], [open_at_broker(None, broker_id="77")], allow_manual=False)
    assert kinds(items) == [("unknown_order", "77")]
    assert items[0].material


def test_an_order_with_a_reference_the_ledger_never_wrote_is_drift():
    items, _ = order_drift([], [open_at_broker("ghost")], allow_manual=True)
    assert kinds(items) == [("unknown_order", "ghost")] and items[0].material


def test_a_working_order_the_ledger_closed_is_drift():
    items, _ = order_drift([ledger("c1", "filled")], [open_at_broker("c1")], allow_manual=True)
    assert kinds(items) == [("order_state", "c1")]
    assert items[0].ours == "filled" and items[0].broker == "working"


def test_a_working_ledger_order_missing_at_the_broker_is_drift():
    items, _ = order_drift([ledger("c1", "accepted")], [], allow_manual=True)
    assert kinds(items) == [("missing_order", "c1")] and items[0].material


def test_unsent_and_unknown_orders_are_not_missing():
    items, _ = order_drift(
        [ledger("c1", "pending"), ledger("c2", "unknown"), ledger("c3", "cancelled")],
        [],
        allow_manual=True,
    )
    assert kinds(items) == [("unresolved_order", "c2")]
    assert not items[0].material


# ---- end of day and status -------------------------------------------------------------


def test_eod_flags_stuck_orders_and_missing_commissions_without_halting():
    items = eod_items(stuck=[ledger("c1", "accepted")], missing_commission=["0001.0001"])
    assert kinds(items) == [("commission_missing", "0001.0001"), ("stuck_order", "c1")]
    assert not any(i.material for i in items)


def test_report_status():
    material = DriftItem(kind="position_qty", key="AAPL.US", ours=1, broker=0, material=True)
    minor = DriftItem(kind="stuck_order", key="c1", ours="accepted", broker=None, material=False)
    explained = DriftItem(
        kind="stale_order", key="c2", ours="accepted", broker=None, material=False, explained=True
    )
    assert report_status([]) == "clean"
    assert report_status([explained]) == "clean"
    assert report_status([minor, explained]) == "warn"
    assert report_status([minor, material]) == "drift"


def test_items_round_trip_to_json_ready_dicts():
    item = DriftItem(kind="position_qty", key="AAPL.US", ours=10, broker=9, material=True,
                     detail="one share short")  # fmt: skip
    assert DriftItem.from_dict(item.to_dict()) == item
