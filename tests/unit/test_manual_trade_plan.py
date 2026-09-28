"""The trade plan on manual orders (roadmap 23.4): stop and target checks,
the protective stop at the broker, and the discipline rule's context."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from stonks.config import RiskPolicy
from stonks.core.types import Order
from stonks.production.live.stops import load_working_stops
from stonks.production.manual import ManualOrderRefused, place_manual_order
from stonks.production.manual_history import manual_context
from stonks.production.manual_stops import pending_manual_stops, sync_manual_stops
from stonks.production.rules.manual_discipline import ManualDisciplineSettings
from stonks.production.rules.settings import RuleSettings
from tests.unit import test_manual_orders as _base
from tests.unit.test_manual_orders import NOW, WorkingBroker, _book, _order

# the manual order fixtures (tmp lake and state, one owner and portfolio)
lake = _base.lake
state = _base.state
owner = _base.owner
portfolio_id = _base.portfolio_id
tick = _base.tick


class FillingBroker(WorkingBroker):
    """Fills market orders at once at 100; keeps stop orders working."""

    def place_order(self, order: Order):
        super().place_order(order)
        if order.order_type == "market":
            st = self.orders[order.client_id]
            self.orders[order.client_id] = replace(
                st, status="filled", filled_quantity=order.quantity, avg_fill_price=100.0
            )
            signed = order.quantity if order.side == "buy" else -order.quantity
            held = self.portfolio.positions.get(order.ticker, 0.0) + signed
            self.portfolio.positions[order.ticker] = held
        self.last = order


def _ctx_json(state, client_id: str) -> dict:
    row = state.sql("SELECT decision_context_json FROM orders WHERE client_id = ?", [client_id])
    return json.loads(row[0]["decision_context_json"])


def test_plan_is_checked_and_kept_on_a_paper_order(state, lake, tick, portfolio_id, owner):
    out = place_manual_order(
        state,
        lake,
        _order(portfolio_id, owner, stop_price=95.0, target_price=115.0),
        _book(portfolio_id, owner),
        tick,
        now=NOW,
    )
    assert out.status == "filled"
    assert out.stop_price == 95.0 and out.reward_risk == pytest.approx(3.0)
    assert out.protective_stop is None  # a simulated book has no broker to hold it
    ctx = _ctx_json(state, out.client_id)
    assert ctx["stop_price"] == 95.0 and ctx["target_price"] == 115.0
    assert ctx["entry_price"] == pytest.approx(100.0)


def test_a_stop_on_the_wrong_side_is_refused(state, lake, tick, portfolio_id, owner):
    with pytest.raises(ManualOrderRefused, match="below the entry"):
        place_manual_order(
            state,
            lake,
            _order(portfolio_id, owner, stop_price=101.0),
            _book(portfolio_id, owner),
            tick,
            preview=True,
            now=NOW,
        )


def test_a_plan_on_an_exit_is_refused(state, lake, tick, portfolio_id, owner):
    book = _book(portfolio_id, owner)
    place_manual_order(state, lake, _order(portfolio_id, owner), book, tick, now=NOW)
    with pytest.raises(ManualOrderRefused, match="closes a position"):
        place_manual_order(
            state,
            lake,
            _order(portfolio_id, owner, side="sell", stop_price=105.0),
            book,
            tick,
            now=NOW,
        )


def test_filled_entry_at_a_broker_gets_a_manual_protective_stop(
    state, lake, tick, portfolio_id, owner
):
    broker = FillingBroker()
    book = _book(portfolio_id, owner, broker=broker)
    out = place_manual_order(
        state,
        lake,
        _order(portfolio_id, owner, stop_price=94.0, client_key="e1"),
        book,
        tick,
        now=NOW,
    )
    assert out.status == "filled"
    assert out.protective_stop == f"{out.client_id}:stop"
    stop = state.sql("SELECT * FROM orders WHERE client_id = ?", [out.protective_stop])[0]
    assert stop["origin"] == "manual" and stop["order_type"] == "stop"
    assert stop["side"] == "sell" and stop["quantity"] == 10.0
    assert stop["stop_price"] == 94.0 and stop["time_in_force"] == "gtc"
    assert stop["oca_group"]
    # the strategy stop sync leaves the person's stop alone
    assert load_working_stops(state, portfolio_id) == []
    # placed once only
    assert pending_manual_stops(state, portfolio_id) == []
    assert sync_manual_stops(state, broker, portfolio_id) == []

    # a manual exit joins the stop's OCA group
    place_manual_order(
        state,
        lake,
        _order(portfolio_id, owner, side="sell", quantity=4.0, client_key="x1"),
        book,
        tick,
        now=NOW,
    )
    assert broker.last.oca_group == stop["oca_group"]


def test_entry_that_fills_later_gets_its_stop_at_the_next_sync(
    state, lake, tick, portfolio_id, owner
):
    broker = WorkingBroker()
    book = _book(portfolio_id, owner, broker=broker)
    out = place_manual_order(
        state,
        lake,
        _order(portfolio_id, owner, order_type="limit", limit_price=99.0, stop_price=95.0),
        book,
        tick,
        now=NOW,
    )
    assert out.status == "pending" and out.protective_stop is None
    st = broker.orders[out.client_id]
    broker.orders[out.client_id] = replace(
        st, status="filled", filled_quantity=10.0, avg_fill_price=99.0
    )
    from stonks.execution.reconcile import reconcile_order

    reconcile_order(broker, state, out.client_id, reject_unknown=False)
    placed = sync_manual_stops(state, broker, portfolio_id)
    assert placed == [f"{out.client_id}:stop"]


def _discipline(**kw) -> RiskPolicy:
    return RiskPolicy(
        rules=RuleSettings(manual_discipline=ManualDisciplineSettings(enabled=True, **kw))
    )


def test_discipline_counts_entries_and_losing_exits(state, lake, tick, portfolio_id, owner):
    book = _book(portfolio_id, owner, risk=_discipline(max_entries_per_day=2))
    place_manual_order(
        state, lake, _order(portfolio_id, owner, client_key="a"), book, tick, now=NOW
    )
    place_manual_order(
        state, lake, _order(portfolio_id, owner, client_key="b", ticker="FLAT.US"), book, tick,
        now=NOW,
    )  # fmt: skip
    with pytest.raises(ManualOrderRefused, match="2 manual entries"):
        place_manual_order(
            state, lake, _order(portfolio_id, owner, client_key="c"), book, tick, now=NOW
        )
    # an exit still passes
    out = place_manual_order(
        state,
        lake,
        _order(portfolio_id, owner, side="sell", client_key="d"),
        book,
        tick,
        now=NOW,
    )
    assert out.status == "filled"
    ctx = manual_context(state, portfolio_id, NOW, live=False, has_stop=False)
    assert ctx.entries_today == 2
    assert ctx.pnl_today <= 1e-6
    tomorrow = manual_context(state, portfolio_id, NOW + timedelta(days=1), live=False,
                              has_stop=False)  # fmt: skip
    assert tomorrow.entries_today == 0


def test_live_entry_without_stop_refused_when_discipline_on(state, lake, tick, portfolio_id, owner):
    book = _book(portfolio_id, owner, risk=_discipline(), broker=FillingBroker(), live=True)
    with pytest.raises(ManualOrderRefused, match="protective stop"):
        place_manual_order(state, lake, _order(portfolio_id, owner), book, tick, preview=True,
                           now=NOW)  # fmt: skip
    ok = place_manual_order(
        state, lake, _order(portfolio_id, owner, stop_price=90.0), book, tick, preview=True,
        now=NOW,
    )  # fmt: skip
    assert ok.status == "preview"


def test_live_stops_job_places_a_pending_manual_stop(state, lake, tick, portfolio_id, owner):
    from stonks.production.live.stops import LiveBook, ProtectiveStopSettings, sync_live_books

    broker = WorkingBroker()
    book = _book(portfolio_id, owner, broker=broker)
    out = place_manual_order(
        state,
        lake,
        _order(portfolio_id, owner, order_type="limit", limit_price=99.0, stop_price=95.0),
        book,
        tick,
        now=NOW,
    )
    st = broker.orders[out.client_id]
    broker.orders[out.client_id] = replace(
        st, status="filled", filled_quantity=10.0, avg_fill_price=99.0
    )
    broker.portfolio.positions["UP.US"] = 10.0
    live = LiveBook(
        portfolio_id=portfolio_id,
        settings_for=lambda _sid: ProtectiveStopSettings(),
        enabled=False,
    )
    result = sync_live_books(state, lake, [live], lambda _pid: broker, as_of=NOW.date())
    assert result[portfolio_id]["manual_placed"] == 1
    assert f"{out.client_id}:stop" in broker.orders
