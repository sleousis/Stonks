"""Drift between the broker and the ledger (roadmap 19.5): pure diffs."""

from __future__ import annotations

import dataclasses

import pytest

from stonks.execution.brokers.base import BrokerOpenOrder
from stonks.execution.drift import (
    BookedFill,
    BrokerStatement,
    CashFlows,
    DriftItem,
    LedgerOrder,
    StatementCash,
    StatementExecution,
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


# ---- edges the mutation run found (roadmap 19.15) ---------------------------------------


def test_every_ticker_is_checked_after_one_that_needs_nothing():
    owned = {"AAA.US": 0.0, "BBB.US": 5.0, "CCC.US": 10.0}
    held = {"AAA.US": 0.0, "BBB.US": 5.0, "CCC.US": 9.0}
    for allow in (True, False):
        items, _ = position_drift(owned, held, allow_manual=allow)
        assert kinds(items) == [("position_qty", "CCC.US")]
    items, external = position_drift(
        {"CCC.US": 10.0}, {"AAA.US": 3.0, "CCC.US": 9.0}, allow_manual=True
    )
    assert kinds(items) == [("position_qty", "CCC.US")] and external == {"AAA.US": 3.0}
    items, _ = position_drift(
        {"AAA.US": 10.0, "BBB.US": 10.0}, {"AAA.US": 9.0, "BBB.US": 8.0}, allow_manual=True
    )
    assert kinds(items) == [("position_qty", "AAA.US"), ("position_qty", "BBB.US")]


def test_near_zero_on_both_sides_is_nothing_at_all():
    items, external = position_drift({"A.US": 1e-12}, {"A.US": 1e-12}, allow_manual=True)
    assert items == [] and external == {}
    items, external = position_drift({"A.US": 1e-9}, {"A.US": 1e-9}, allow_manual=True)
    assert items == [] and external == {}


def test_a_larger_opposite_holding_never_covers_what_stonks_owns():
    items, external = position_drift({"A.US": 10.0}, {"A.US": -15.0}, allow_manual=True)
    assert kinds(items) == [("position_qty", "A.US")]
    assert external == {"A.US": -15.0}


def test_fractional_positions_match():
    items, external = position_drift({"BTC.CC": 0.5}, {"BTC.CC": 0.5}, allow_manual=True)
    assert items == [] and external == {}


def test_a_short_fall_leaves_nothing_external():
    items, external = position_drift({"A.US": 10.0}, {"A.US": 5.0}, allow_manual=True)
    assert kinds(items) == [("position_qty", "A.US")] and external == {}


def test_position_details_name_the_cause():
    items, _ = position_drift({"A.US": 10.0}, {"A.US": 9.0, "B.US": 2.0}, allow_manual=False)
    by_kind = {i.kind: i.detail for i in items}
    assert by_kind["position_qty"] == "Stonks owns 10, the broker holds 9"
    assert "never traded" in by_kind["unknown_position"]


def test_every_broker_order_is_checked_after_a_hand_placed_one():
    manual = BrokerOpenOrder(
        broker_order_id="m1", client_id=None, ticker="MSFT.US", side="buy", quantity=1
    )
    items, external = order_drift(
        [], [manual, open_at_broker("ghost", broker_id="2")], allow_manual=True
    )
    assert kinds(items) == [("unknown_order", "ghost")] and external == [manual]


def test_a_missing_order_is_material():
    [item] = order_drift([ledger("c1")], [], allow_manual=True)[0]
    assert item.kind == "missing_order" and item.material


def test_an_item_without_explained_reads_as_unexplained():
    item = DriftItem.from_dict({"kind": "cash", "key": "USD", "material": False})
    assert item.explained is False and item.ours is None and item.detail == ""


def test_the_values_are_frozen():
    values = [
        DriftItem(kind="cash", key="USD", ours=1, broker=2, material=False),
        ledger("c1"),
        CashFlows(),
        StatementExecution(exec_id="e", quantity=1.0),
        StatementCash(kind="other", amount=1.0, date=None),
        BrokerStatement("U1", None, None),
        BookedFill("e", "A.US", 1.0, None, None),
    ]
    for value in values:
        name = dataclasses.fields(value)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, name, None)
    assert StatementCash("other", 1.0, None) == StatementCash("other", 1.0, None)
    assert BrokerStatement("U1", None, None) == BrokerStatement("U1", None, None)
