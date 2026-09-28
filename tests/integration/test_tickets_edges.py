"""Order ticket reads and status changes at their edges (roadmap 19.8):
filters, the submit window per portfolio, counts per owner, repeated and
empty decisions."""

from __future__ import annotations

import pytest

import tests.integration.test_tick_modes as modes
from stonks.production.tickets import (
    IllegalTicketTransition,
    TicketRefused,
    awaiting_counts,
    decide_tickets,
    due_tickets,
    list_tickets,
    set_ticket_status,
)
from tests.integration.test_tick_tickets import DAY1, IN_WINDOW, _tick


@pytest.fixture(autouse=True)
def _clean_fakes():
    modes.fake.FAKE_BOOKS.clear()
    modes.reset_limiters()
    yield
    modes.fake.FAKE_BOOKS.clear()
    modes.reset_limiters()


@pytest.fixture
def world(tmp_path, lake_trending):
    w = modes.World(tmp_path, lake_trending)
    w.state.execute("UPDATE subscriptions SET mode = 'approve' WHERE id = ?", [w.bob_auto])
    _tick(w, DAY1)  # one ticket waiting for Bob
    yield w
    w.state.close()


def test_list_tickets_filters_by_status_tick_and_limit(world):
    [t] = list_tickets(world.state)
    assert list_tickets(world.state, status="awaiting_approval") == [t]
    assert list_tickets(world.state, status="approved") == []
    assert list_tickets(world.state, tick_id=t.tick_id) == [t]
    assert list_tickets(world.state, tick_id="tick_other") == []
    assert list_tickets(world.state, limit=0) == []
    assert list_tickets(world.state, portfolio_ids=[]) == []


def test_counts_of_waiting_tickets_per_owner(world):
    [t] = list_tickets(world.state)
    assert awaiting_counts(world.state) == {t.portfolio_id: 1}
    assert awaiting_counts(world.state, owner_id=world.bob.user_id) == {t.portfolio_id: 1}
    assert awaiting_counts(world.state, owner_id="usr_nobody") == {}


def test_due_tickets_of_one_portfolio(world):
    [t] = list_tickets(world.state)
    decide_tickets(world.state, [t.id], approve=True, actor="bob", now=t.submit_after)
    assert [d.id for d in due_tickets(world.state, IN_WINDOW, portfolio_id=t.portfolio_id)] == [
        t.id
    ]
    assert due_tickets(world.state, IN_WINDOW, portfolio_id="pf_other") == []


def test_the_same_status_again_changes_nothing(world):
    [t] = list_tickets(world.state)
    again = set_ticket_status(world.state, t.id, "awaiting_approval", now=IN_WINDOW, reason="x")
    assert again == t
    with pytest.raises(IllegalTicketTransition, match="cannot go from awaiting_approval"):
        set_ticket_status(world.state, t.id, "filled", now=IN_WINDOW)


def test_deciding_nothing_is_refused(world):
    with pytest.raises(TicketRefused, match="no tickets to decide"):
        decide_tickets(world.state, [], approve=True, actor="bob", now=IN_WINDOW)
