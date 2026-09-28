"""Manual orders at their edges (roadmap 20.1): bad input, a broker that
cannot look orders up or cancel, short books that would flip, a rule that
reads history, not enough cash, a live context that fails to build, and
every refusal of a change."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from stonks.config import RiskPolicy
from stonks.core.types import Portfolio
from stonks.production.manual import (
    ManualOrderRefused,
    cancel_order,
    change_manual_order,
    place_manual_order,
)
from stonks.production.rules.settings import RuleSettings
from tests.unit import test_manual_orders as base
from tests.unit.test_manual_orders import NOW, WorkingBroker, _book, _order

# the fixtures of the main manual order tests
lake, state, owner, portfolio_id, tick = (
    base.lake, base.state, base.owner, base.portfolio_id, base.tick,
)  # fmt: skip

LIMIT = {"order_type": "limit", "limit_price": 95.0}


def place(state, lake, tick, book, order, **kw):
    return place_manual_order(state, lake, order, book, tick, now=kw.pop("now", NOW), **kw)


# ---- input ---------------------------------------------------------------------------


@pytest.mark.parametrize("qty", [0.0, -3.0])
def test_a_quantity_must_be_positive(state, lake, tick, portfolio_id, owner, qty):
    with pytest.raises(ManualOrderRefused, match="quantity must be positive"):
        place(state, lake, tick, _book(portfolio_id, owner), _order(portfolio_id, owner,
                                                                    quantity=qty))  # fmt: skip


def test_without_a_time_the_order_is_checked_now(state, lake, tick, portfolio_id, owner):
    # the lake's closes end in April 2026: today they are stale
    with pytest.raises(ManualOrderRefused, match=r"no recent close for UP.US"):
        place_manual_order(state, lake, _order(portfolio_id, owner), _book(portfolio_id, owner),
                           tick)  # fmt: skip


# ---- the broker -------------------------------------------------------------------------


class BlindBroker:
    """Takes orders but cannot look them up by client id."""

    def fetch_portfolio(self):
        return Portfolio(cash=50_000.0)

    def place_order(self, order):
        return None

    def reconcile(self):
        return []


def test_a_broker_that_cannot_look_orders_up_is_refused(state, lake, tick, portfolio_id, owner):
    book = _book(portfolio_id, owner, broker=BlindBroker())
    with pytest.raises(ManualOrderRefused, match="cannot look orders up"):
        place(state, lake, tick, book, _order(portfolio_id, owner))
    assert state.sql("SELECT client_id FROM orders") == []


def test_a_sync_that_fails_after_the_send_keeps_the_order_pending(
    state, lake, tick, portfolio_id, owner
):
    class Flaky(WorkingBroker):
        def get_order_state(self, client_id: str):
            raise ConnectionError("dropped")

    out = place(state, lake, tick, _book(portfolio_id, owner, broker=Flaky()),
                _order(portfolio_id, owner, **LIMIT))  # fmt: skip
    assert out.status == "pending"
    [row] = state.sql("SELECT state FROM orders WHERE client_id = ?", [out.client_id])
    assert row["state"] == "submitted"


def test_a_live_context_that_cannot_be_built_refuses_nothing_silently(
    state, lake, tick, portfolio_id, owner, monkeypatch
):
    import stonks.production.live.context as live_context

    def boom(*args, **kwargs):
        raise RuntimeError("account read failed")

    monkeypatch.setattr(live_context, "build_live_context", boom)
    out = place(state, lake, tick, _book(portfolio_id, owner, broker=WorkingBroker()),
                _order(portfolio_id, owner, **LIMIT))  # fmt: skip
    # with no live limits set, an empty context changes nothing
    assert out.status == "pending"


# ---- short books ------------------------------------------------------------------------


def test_a_short_book_refuses_an_order_that_flips_the_position(
    state, lake, tick, portfolio_id, owner
):
    book = _book(portfolio_id, owner, allow_short=True)
    place(state, lake, tick, book, _order(portfolio_id, owner, quantity=5.0))
    with pytest.raises(ManualOrderRefused, match="close a position and open the other way"):
        place(state, lake, tick, book, _order(portfolio_id, owner, side="sell", quantity=8.0))


def test_a_short_book_opens_a_short_from_flat(state, lake, tick, portfolio_id, owner):
    book = _book(portfolio_id, owner, allow_short=True)
    out = place(state, lake, tick, book, _order(portfolio_id, owner, side="sell", quantity=3.0))
    assert out.status == "filled"
    [row] = state.sql("SELECT client_id, position_effect FROM orders")
    assert (row["client_id"], row["position_effect"]) == (out.client_id, "open")
    [snap] = state.sql("SELECT positions_json FROM portfolio_snapshots")
    assert json.loads(snap["positions_json"]) == {"UP.US": -3.0}


def test_a_short_sale_is_idempotent_by_its_key(state, lake, tick, portfolio_id, owner):
    book = _book(portfolio_id, owner, allow_short=True)
    order = _order(portfolio_id, owner, side="sell", quantity=3.0, client_key="k1")
    first = place(state, lake, tick, book, order)
    again = place(state, lake, tick, book, order)
    assert again.duplicate and again.client_id == first.client_id
    assert state.sql("SELECT COUNT(*) FROM fills")[0][0] == 1


# ---- risk and cash ----------------------------------------------------------------------


def test_a_rule_that_reads_history_sees_the_lake(state, lake, tick, portfolio_id, owner):
    # median dollar volume 100 * 1,000,000: 0.0001% of it is 100 dollars, one share
    risk = RiskPolicy(rules=RuleSettings.model_validate({"liquidity": {"max_pct_adv": 1e-6}}))
    book = _book(portfolio_id, owner, risk=risk)
    with pytest.raises(ManualOrderRefused, match="allow 1 of the 10"):
        place(state, lake, tick, book, _order(portfolio_id, owner))
    out = place(state, lake, tick, book, _order(portfolio_id, owner, allow_reduce=True))
    assert (out.status, out.quantity) == ("filled", 1.0)


def test_not_enough_cash_at_the_close_is_recorded_rejected(
    state, lake, tick, portfolio_id, owner, monkeypatch
):
    from stonks.backtest.simulated_broker import SimulatedBroker

    monkeypatch.setattr(SimulatedBroker, "place_order", lambda self, order, *a, **k: None)
    out = place(state, lake, tick, _book(portfolio_id, owner), _order(portfolio_id, owner))
    assert out.status == "rejected" and out.fill_price is None
    assert out.reason == "not enough cash for this order at the latest close"
    assert state.sql("SELECT COUNT(*) FROM fills")[0][0] == 0


# ---- cancel and change ------------------------------------------------------------------


def working(state, lake, tick, portfolio_id, owner, broker=None, **kw):
    broker = broker or WorkingBroker()
    book = _book(portfolio_id, owner, broker=broker)
    out = place(state, lake, tick, book, _order(portfolio_id, owner, **(LIMIT | kw)))
    return book, broker, out.client_id


def change(state, lake, tick, book, cid, **kw):
    return change_manual_order(state, lake, cid, book, tick, actor="user:x", reason="r",
                               now=NOW, **kw)  # fmt: skip


def test_a_broker_that_cannot_cancel_is_refused(state, lake, tick, portfolio_id, owner):
    _, _, cid = working(state, lake, tick, portfolio_id, owner)
    with pytest.raises(ManualOrderRefused, match="cannot cancel orders"):
        cancel_order(state, portfolio_id, cid, broker=BlindBroker(), actor="user:x",  # type: ignore[arg-type]
                     reason="r")  # fmt: skip


def test_a_cancel_the_broker_refuses_leaves_no_reason(state, lake, tick, portfolio_id, owner):
    _, broker, cid = working(state, lake, tick, portfolio_id, owner)
    broker.orders[cid] = replace(broker.orders[cid], status="partially_filled",
                                 filled_quantity=2.0, avg_fill_price=95.0)  # fmt: skip
    done = cancel_order(state, portfolio_id, cid, broker=broker, actor="user:x", reason="r")
    assert not done.cancelled and done.status == "partially_filled"
    [row] = state.sql("SELECT status_reason FROM orders WHERE client_id = ?", [cid])
    assert row["status_reason"] is None


def test_change_refuses_a_finished_order(state, lake, tick, portfolio_id, owner):
    book, broker, cid = working(state, lake, tick, portfolio_id, owner)
    cancel_order(state, portfolio_id, cid, broker=broker, actor="user:x", reason="r")
    with pytest.raises(ManualOrderRefused, match="is cancelled; only a working order"):
        change(state, lake, tick, book, cid, quantity=5.0)


def test_change_needs_something_to_change(state, lake, tick, portfolio_id, owner):
    book, _, cid = working(state, lake, tick, portfolio_id, owner)
    with pytest.raises(ManualOrderRefused, match="new quantity or a new limit price"):
        change(state, lake, tick, book, cid)


def test_only_a_limit_order_changes_its_limit(state, lake, tick, portfolio_id, owner):
    book, _, cid = working(state, lake, tick, portfolio_id, owner, order_type="market",
                           limit_price=None)  # fmt: skip
    with pytest.raises(ManualOrderRefused, match="only a limit order has a limit price"):
        change(state, lake, tick, book, cid, limit_price=90.0)


def test_change_below_what_already_filled_is_refused(state, lake, tick, portfolio_id, owner):
    from stonks.execution.reconcile import reconcile_order

    book, broker, cid = working(state, lake, tick, portfolio_id, owner)
    broker.orders[cid] = replace(broker.orders[cid], status="partially_filled",
                                 filled_quantity=6.0, avg_fill_price=95.0)  # fmt: skip
    reconcile_order(broker, state, cid)
    with pytest.raises(ManualOrderRefused, match="6 already filled"):
        change(state, lake, tick, book, cid, quantity=5.0)


def test_change_stops_when_the_broker_does_not_cancel(state, lake, tick, portfolio_id, owner):
    class NoCancel(WorkingBroker):
        def cancel_order(self, client_id: str) -> bool:
            return False

    book, _, cid = working(state, lake, tick, portfolio_id, owner, broker=NoCancel())
    with pytest.raises(ManualOrderRefused, match="did not cancel"):
        change(state, lake, tick, book, cid, quantity=5.0)
    assert [r[0] for r in state.sql("SELECT client_id FROM orders")] == [cid]


def test_change_stops_when_the_order_filled_before_the_cancel(
    state, lake, tick, portfolio_id, owner
):
    class FillsFirst(WorkingBroker):
        def cancel_order(self, client_id: str) -> bool:
            st = self.orders[client_id]
            self.orders[client_id] = replace(st, status="cancelled", filled_quantity=st.quantity,
                                             avg_fill_price=95.0)  # fmt: skip
            return True

    book, _, cid = working(state, lake, tick, portfolio_id, owner, broker=FillsFirst())
    with pytest.raises(ManualOrderRefused, match="10 filled before the cancel"):
        change(state, lake, tick, book, cid, quantity=10.0)
    assert [r[0] for r in state.sql("SELECT client_id FROM orders")] == [cid]


def test_a_short_sale_at_a_broker_is_sent_once_and_cancelled_by_its_id(
    state, lake, tick, portfolio_id, owner
):
    class Counting(WorkingBroker):
        sent = 0

        def place_order(self, order):
            Counting.sent += 1
            return super().place_order(order)

    broker = Counting()
    book = _book(portfolio_id, owner, broker=broker, allow_short=True)
    order = _order(portfolio_id, owner, side="sell", quantity=3.0, client_key="k2", **LIMIT)
    first = place(state, lake, tick, book, order)
    again = place(state, lake, tick, book, order)
    assert again.duplicate and again.client_id == first.client_id
    assert Counting.sent == 1
    done = cancel_order(state, portfolio_id, first.client_id, broker=broker, actor="user:x",
                        reason="r")  # fmt: skip
    assert done.cancelled
