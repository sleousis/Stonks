"""Execution algos through the live ticket path (roadmap 23.16).

A portfolio's algo setting rides on its tickets. At a broker without the
algo (the fake trading provider), a TWAP ticket becomes a parent whose
child slices the ``algo_slices`` job sends inside the window, through
halts, reconciliation and the ticket's outcome."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import tests.integration.test_tick_modes as modes
from stonks.core.clock import FakeClock
from stonks.execution.algos.settings import set_setting
from stonks.production.algo_slices import get_parent, open_parent_count, work_parents
from stonks.production.halts import trip_halt
from stonks.production.tca import load_order_tca, summarize
from stonks.production.tickets import decide_tickets, list_tickets, sync_submitted
from tests.integration.test_tick_tickets import DAY1, IN_WINDOW, OPEN, _approve, _submit, _tick


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
    yield w
    w.state.close()


def _open_broker(world):
    from stonks.accounts import PortfolioRepository, Scope

    def open_broker(portfolio_id: str):
        return world.traders(PortfolioRepository(world.state).get(Scope.service("t"), portfolio_id))

    return open_broker


def _ticket_with(world, algo: str, params: dict) -> object:
    set_setting(world.state, world.live, algo, params, actor="user:bob", now=IN_WINDOW)
    _approve(world)
    _tick(world, DAY1)
    [ticket] = list_tickets(world.state, portfolio_ids=[world.live])
    decide_tickets(world.state, [ticket.id], approve=True, actor="user:bob", now=IN_WINDOW)
    return ticket


def _work(world, when: datetime):
    return work_parents(world.state, _open_broker(world), clock=FakeClock(when))


def test_a_twap_ticket_is_sent_as_child_slices_in_its_window(world):
    ticket = _ticket_with(world, "twap", {"end_minutes": 60, "slices": 3})
    assert ticket.order.algo == {
        "name": "twap",
        "params": {"start_minutes": 0, "end_minutes": 60, "slices": 3},
    }
    assert ticket.preview["algo"]["name"] == "twap"

    done = _submit(world, IN_WINDOW)
    assert done.sent == 1
    assert world.book.orders == {}  # the parent never reaches the broker
    parent = get_parent(world.state, ticket.client_id)
    assert (parent.algo, parent.state, len(parent.slices)) == ("twap", "accepted", 3)
    assert parent.window_start == OPEN
    assert open_parent_count(world.state) == 1

    assert _work(world, OPEN - timedelta(minutes=5)).sent == 0  # not yet due
    first = _work(world, OPEN + timedelta(minutes=1))
    assert first.sent == 1
    assert list(world.book.orders) == [f"{ticket.client_id}.s01"]
    assert get_parent(world.state, ticket.client_id).state == "partially_filled"

    _work(world, OPEN + timedelta(minutes=21))
    _work(world, OPEN + timedelta(minutes=41))
    parent = get_parent(world.state, ticket.client_id)
    assert parent.state == "filled"
    assert parent.filled == pytest.approx(parent.quantity)
    assert [s["status"] for s in parent.slices] == ["sent", "sent", "sent"]

    child = world.state.sql(
        "SELECT exec_algo, parent_client_id, time_in_force FROM orders WHERE client_id = ?",
        [f"{ticket.client_id}.s02"],
    )[0]
    assert tuple(child) == ("twap", ticket.client_id, "day")

    assert sync_submitted(world.state, now=OPEN + timedelta(minutes=42)) == 1
    [after] = list_tickets(world.state, portfolio_ids=[world.live])
    assert after.status == "filled"
    assert open_parent_count(world.state) == 0

    # TCA measures each algo
    rows = load_order_tca(world.state, world.live)
    [group] = [g for g in summarize(rows, "algo") if g.key == "twap"]
    assert group.orders == 3


def test_a_rerun_of_the_submit_does_not_start_the_parent_twice(world):
    ticket = _ticket_with(world, "twap", {"end_minutes": 60, "slices": 2})
    _submit(world, IN_WINDOW)
    again = _submit(world, IN_WINDOW + timedelta(minutes=1))
    assert again.sent == 0
    rows = world.state.sql(
        "SELECT COUNT(*) FROM algo_parents WHERE client_id = ?", [ticket.client_id]
    )
    assert rows[0][0] == 1


def test_a_kill_switch_holds_the_slices_and_the_window_end_skips_them(world):
    ticket = _ticket_with(world, "vwap", {"end_minutes": 30, "slices": 2})
    _submit(world, IN_WINDOW)
    trip_halt(world.state, "kill", reason="drill", actor="t", portfolio_id=world.live,
              halt="all", on=OPEN.date())  # fmt: skip
    held = _work(world, OPEN + timedelta(minutes=20))
    assert (held.sent, held.portfolios[0].held) == (0, 2)
    assert world.book.orders == {}

    late = _work(world, OPEN + timedelta(minutes=31))
    assert late.portfolios[0].skipped == 2
    parent = get_parent(world.state, ticket.client_id)
    assert parent.state == "expired"
    sync_submitted(world.state, now=OPEN + timedelta(minutes=32))
    [after] = list_tickets(world.state, portfolio_ids=[world.live])
    assert after.status == "unfilled"


def test_an_adaptive_ticket_goes_out_plain_at_a_broker_without_it(world):
    ticket = _ticket_with(world, "adaptive", {"priority": "patient"})
    done = _submit(world, IN_WINDOW)
    assert done.sent == 1
    assert list(world.book.orders) == [ticket.client_id]
    row = world.state.sql("SELECT exec_algo FROM orders WHERE client_id = ?", [ticket.client_id])
    assert row[0]["exec_algo"] is None


def test_a_strategy_setting_wins_over_the_portfolio_setting(world):
    set_setting(world.state, world.live, "adaptive", {}, actor="t", now=IN_WINDOW)
    set_setting(world.state, world.live, "twap", {"slices": 2}, strategy_id="bh_up", actor="t",
                now=IN_WINDOW)  # fmt: skip
    _approve(world)
    _tick(world, DAY1)
    [ticket] = list_tickets(world.state, portfolio_ids=[world.live])
    assert ticket.order.algo["name"] == "twap"  # type: ignore[index]


def test_no_setting_keeps_plain_orders(world):
    _approve(world)
    _tick(world, DAY1)
    [ticket] = list_tickets(world.state, portfolio_ids=[world.live])
    assert ticket.order.algo is None
    assert "algo" not in ticket.preview


def test_an_empty_window_fails_the_ticket(world):
    _ticket_with(world, "twap", {"start_minutes": 1000})
    done = _submit(world, IN_WINDOW)
    assert done.failed == 1
    [after] = list_tickets(world.state, portfolio_ids=[world.live])
    assert after.status == "failed" and "empty" in (after.status_reason or "")


def test_work_parents_reports_a_broker_it_cannot_open(world):
    _ticket_with(world, "twap", {"slices": 2})
    _submit(world, IN_WINDOW)

    def broken(_pid):
        raise RuntimeError("gateway down")

    result = work_parents(world.state, broken, clock=FakeClock(OPEN + timedelta(minutes=5)))
    assert result.errors == 1 and result.detail()["errors"] == [
        f"{world.live}: RuntimeError: gateway down"
    ]


def test_nothing_to_work_is_a_no_op(world):
    assert work_parents(world.state, _open_broker(world),
                        clock=FakeClock(datetime(2026, 3, 18, tzinfo=UTC))).portfolios == ()  # fmt: skip
