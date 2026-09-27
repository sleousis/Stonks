"""Order tickets (roadmap 19.8): what a live book decided after the close,
waiting for a person (approve mode, runaway runs) or already approved by
the system (auto), and sent in the submit window."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from stonks.core.types import Order
from stonks.production.live.settings import SubmitSettings
from stonks.production.tickets import (
    SYSTEM_ACTOR,
    IllegalTicketTransition,
    TicketRefused,
    decide_tickets,
    due_tickets,
    expire_due,
    get_ticket,
    list_tickets,
    set_ticket_status,
    submit_window,
    sync_submitted,
    write_tickets,
)
from stonks.store.state import SqliteState

AS_OF = date(2026, 3, 17)  # a Tuesday; New York is on summer time
OPEN = datetime(2026, 3, 18, 13, 30, tzinfo=UTC)
DECIDED = datetime(2026, 3, 17, 20, 45, tzinfo=UTC)
WINDOW = submit_window(AS_OF, SubmitSettings())


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


def _order(ticker: str, side: str = "buy", qty: float = 10.0, **kw) -> Order:
    return Order(
        client_id=f"2026-03-17:{ticker}:{side}",
        ticker=ticker,
        side=side,  # type: ignore[arg-type]
        quantity=qty,
        strategy_id="s1",
        portfolio_id="pf_default",
        decision_price=100.0,
        decided_at=datetime(2026, 3, 17, tzinfo=UTC),
        decision_context={"score": 0.4, "rank": 1},
        **kw,
    )


def _write(state, orders, hold=None):
    return write_tickets(
        state,
        orders,
        portfolio_id="pf_default",
        tick_id=None,
        as_of=AS_OF,
        window=WINDOW,
        hold=lambda order: hold,
        now=DECIDED,
    )


def test_the_submit_window_leads_up_to_the_next_open():
    assert WINDOW.submit_after == OPEN - timedelta(minutes=20)
    assert WINDOW.expires_at == OPEN - timedelta(minutes=2)
    friday = submit_window(date(2026, 3, 20), SubmitSettings(window_minutes=30))
    assert friday.submit_after == datetime(2026, 3, 23, 13, 0, tzinfo=UTC)  # Monday


def test_auto_tickets_are_approved_by_the_system(state):
    [ticket] = _write(state, [_order("A.US")])
    assert ticket.status == "approved" and ticket.hold is None
    assert ticket.decided_by == SYSTEM_ACTOR
    assert ticket.order == _order("A.US")
    assert ticket.order.decision_context == {"score": 0.4, "rank": 1}
    assert ticket.preview["notional"] == pytest.approx(1000.0)
    assert ticket.reason["score"] == 0.4
    assert (ticket.submit_after, ticket.expires_at) == (WINDOW.submit_after, WINDOW.expires_at)


def test_approve_tickets_wait_and_a_rewrite_changes_nothing(state):
    [ticket] = _write(state, [_order("A.US")], hold="approve_mode")
    assert ticket.status == "awaiting_approval" and ticket.decided_by is None
    assert _write(state, [_order("A.US")], hold=None) == []  # same client id: kept as is
    assert get_ticket(state, ticket.id).status == "awaiting_approval"


def test_approve_and_reject(state):
    a, b = _write(state, [_order("A.US"), _order("B.US")], hold="approve_mode")
    [approved] = decide_tickets(
        state, [a.id], approve=True, actor="user:alice", now=DECIDED + timedelta(hours=1)
    )
    assert approved.status == "approved" and approved.decided_by == "user:alice"
    with pytest.raises(TicketRefused, match="reason"):
        decide_tickets(state, [b.id], approve=False, actor="user:alice", now=DECIDED)
    [rejected] = decide_tickets(
        state, [b.id], approve=False, actor="user:alice", reason="too big", now=DECIDED
    )
    assert rejected.status == "rejected" and rejected.decision_reason == "too big"
    with pytest.raises(TicketRefused):  # decided already
        decide_tickets(state, [a.id], approve=True, actor="user:alice", now=DECIDED)
    audit = state.sql("SELECT action, target_id FROM audit_log ORDER BY id")
    assert [tuple(r) for r in audit] == [("ticket.approve", a.id), ("ticket.reject", b.id)]


def test_a_batch_is_all_or_nothing(state):
    a, b = _write(state, [_order("A.US"), _order("B.US")], hold="approve_mode")
    decide_tickets(state, [b.id], approve=False, actor="user:alice", reason="no", now=DECIDED)
    with pytest.raises(TicketRefused):
        decide_tickets(state, [a.id, b.id], approve=True, actor="user:alice", now=DECIDED)
    assert get_ticket(state, a.id).status == "awaiting_approval"


def test_a_ticket_past_its_deadline_cannot_be_approved(state):
    [ticket] = _write(state, [_order("A.US")], hold="approve_mode")
    with pytest.raises(TicketRefused, match="expired"):
        decide_tickets(state, [ticket.id], approve=True, actor="u", now=WINDOW.expires_at)


def test_expire_due(state):
    held, auto = (
        _write(state, [_order("A.US")], hold="approve_mode")[0],
        _write(state, [_order("B.US")])[0],
    )
    assert expire_due(state, WINDOW.expires_at - timedelta(seconds=1)) == []
    assert sorted(expire_due(state, WINDOW.expires_at)) == sorted([held.id, auto.id])
    assert {t.status for t in list_tickets(state)} == {"expired"}


def test_due_tickets_are_the_approved_ones_inside_the_window(state):
    [auto] = _write(state, [_order("A.US")])
    _write(state, [_order("B.US")], hold="approve_mode")
    assert due_tickets(state, WINDOW.submit_after - timedelta(minutes=1)) == []
    assert [t.id for t in due_tickets(state, WINDOW.submit_after)] == [auto.id]
    assert due_tickets(state, WINDOW.expires_at) == []


def test_transitions_follow_the_table(state):
    [ticket] = _write(state, [_order("A.US")], hold="approve_mode")
    with pytest.raises(IllegalTicketTransition):
        set_ticket_status(state, ticket.id, "submitted", now=DECIDED)
    decide_tickets(state, [ticket.id], approve=True, actor="u", now=DECIDED)
    set_ticket_status(state, ticket.id, "submitted", now=DECIDED)
    with pytest.raises(IllegalTicketTransition):
        set_ticket_status(state, ticket.id, "approved", now=DECIDED)


def _order_row(state, client_id: str, status: str, state_: str) -> None:
    state.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status, state,"
        " created_at, updated_at, portfolio_id) VALUES (?, 'A.US', 'buy', 10, 'market', ?, ?,"
        " 'x', 'x', 'pf_default')",
        [client_id, status, state_],
    )


@pytest.mark.parametrize(
    ("status", "fine", "ticket_status"),
    [
        ("filled", "filled", "filled"),
        ("cancelled", "expired", "unfilled"),
        ("cancelled", "cancelled", "cancelled"),
        ("rejected", "rejected", "failed"),
        ("pending", "accepted", "submitted"),
    ],
)
def test_submitted_tickets_follow_their_order(state, status, fine, ticket_status):
    [ticket] = _write(state, [_order("A.US")])
    set_ticket_status(state, ticket.id, "submitted", now=DECIDED)
    _order_row(state, ticket.client_id, status, fine)
    sync_submitted(state, now=DECIDED)
    assert get_ticket(state, ticket.id).status == ticket_status
