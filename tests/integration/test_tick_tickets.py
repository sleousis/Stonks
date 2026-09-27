"""Decide after the close, submit in the window (roadmap 19.8).

A live book writes order tickets instead of sending orders when it has an
``approve`` subscription, when ``[production.live] submit_in_window`` is on,
or when a runaway run or halt holds its closes. The ``live_submit`` job
(:func:`stonks.production.submit.submit_tickets`) sends the approved ones
in the window before the next open, after the startup reconciliation gate.
The tick's own order rows carry the fine order state."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

import tests.integration.test_tick_modes as modes
from stonks.connections.base import ProviderError
from stonks.core.clock import FakeClock
from stonks.production.halts import trip_halt
from stonks.production.live.settings import LiveSettings
from stonks.production.portfolio_runs import list_runs
from stonks.production.submit import submit_tickets
from stonks.production.tick import TickSettings, load_tick_plan, run_tick
from stonks.production.tickets import decide_tickets, list_tickets

DAY1, DAY2 = modes.DAY1, modes.DAY2
#: The window before the open after DAY1 (New York summer time).
OPEN = datetime(2026, 3, 18, 13, 30, tzinfo=UTC)
IN_WINDOW = OPEN - timedelta(minutes=15)
LATE = OPEN - timedelta(minutes=1)


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


def _tick(world, as_of, settings: TickSettings = modes.SETTINGS, traders=None, **kw):
    plan = load_tick_plan(world.state, settings, traders=traders or world.traders)
    return run_tick(world.state, world.lake, world.registry, settings, as_of=as_of, plan=plan, **kw)


def _submit(world, when: datetime, traders=None):
    open_trader = traders or world.traders

    def open_broker(portfolio_id: str):
        from stonks.accounts import PortfolioRepository, Scope

        return open_trader(PortfolioRepository(world.state).get(Scope.service("t"), portfolio_id))

    return submit_tickets(world.state, open_broker, clock=FakeClock(when))


def _approve(world):
    world.state.execute("UPDATE subscriptions SET mode = 'approve' WHERE id = ?", [world.bob_auto])


def _live_row(world, client_id):
    rows = world.state.sql("SELECT status, state FROM orders WHERE client_id = ?", [client_id])
    return tuple(rows[0]) if rows else None


# ---- approve mode ------------------------------------------------------------------


def test_an_approve_book_writes_tickets_and_sends_nothing(world):
    _approve(world)
    result = _tick(world, DAY1)

    assert world.book.orders == {} and "place_order" not in world.book.calls
    assert world.orders(world.live) == []
    [ticket] = list_tickets(world.state, portfolio_ids=[world.live])
    assert (ticket.ticker, ticket.side, ticket.strategy_id) == ("UP.US", "buy", "bh_up")
    assert ticket.status == "awaiting_approval" and ticket.hold == "approve_mode"
    assert ticket.client_id == f"2026-03-17:{world.live}:bh_up:UP.US:buy"
    assert ticket.expires_at == OPEN - timedelta(minutes=2)
    [live] = [r for r in result.portfolios if r.portfolio_id == world.live]
    assert live.summary["tickets"] == {"written": 1, "awaiting_approval": 1, "approved": 0}
    assert live.orders_placed == 0

    # a high-urgency push with no amounts or tickers
    [push] = world.state.sql("SELECT * FROM notification_outbox WHERE category = 'order'")
    assert push["user_id"] == world.bob.user_id and push["urgency"] == "high"
    assert "1 order waits for approval" in push["body"] and "UP.US" not in push["body"]
    [run] = list_runs(world.state, world.live)
    assert run.mode == "auto" and run.auto_subscriptions == (world.bob_auto,)

    _tick(world, DAY1)  # a same-day re-run writes no second ticket
    assert len(list_tickets(world.state, portfolio_ids=[world.live])) == 1


def test_an_approved_ticket_is_sent_in_the_window(world):
    _approve(world)
    _tick(world, DAY1)
    [ticket] = list_tickets(world.state, portfolio_ids=[world.live])
    decide_tickets(world.state, [ticket.id], approve=True, actor="user:bob", now=IN_WINDOW)

    early = _submit(world, OPEN - timedelta(minutes=25))
    assert early.sent == 0 and world.book.orders == {}

    done = _submit(world, IN_WINDOW)
    assert done.sent == 1
    assert list(world.book.orders) == [ticket.client_id]
    assert _live_row(world, ticket.client_id) == ("filled", "filled")
    [after] = list_tickets(world.state, portfolio_ids=[world.live])
    assert after.status == "filled" and after.submitted_at is not None

    again = _submit(world, IN_WINDOW + timedelta(minutes=1))
    assert again.sent == 0 and len(world.book.orders) == 1


def test_rejected_and_undecided_tickets_are_never_sent(world):
    _approve(world)
    _tick(world, DAY1)
    [ticket] = list_tickets(world.state, portfolio_ids=[world.live])
    assert _submit(world, IN_WINDOW).sent == 0
    late = _submit(world, LATE)
    assert late.expired == (ticket.id,)
    assert list_tickets(world.state, portfolio_ids=[world.live])[0].status == "expired"
    assert world.book.orders == {}


# ---- auto books in the window --------------------------------------------------------


def test_auto_books_can_decide_now_and_submit_later(world):
    settings = replace(modes.SETTINGS, live=LiveSettings(submit_in_window=True))
    _tick(world, DAY1, settings)
    assert world.book.orders == {}
    [ticket] = list_tickets(world.state, portfolio_ids=[world.live])
    assert ticket.status == "approved" and ticket.decided_by == "service:system"
    assert world.state.sql("SELECT * FROM notification_outbox WHERE category = 'order'") == []

    assert _submit(world, IN_WINDOW).sent == 1
    assert _live_row(world, ticket.client_id) == ("filled", "filled")


def test_a_kill_switch_holds_the_submit(world):
    settings = replace(modes.SETTINGS, live=LiveSettings(submit_in_window=True))
    _tick(world, DAY1, settings)
    trip_halt(world.state, "kill", reason="stop", actor="t", portfolio_id=world.live, halt="all")
    result = _submit(world, IN_WINDOW)
    assert result.sent == 0 and result.portfolios[0].held == 1
    assert world.book.orders == {}
    assert list_tickets(world.state, portfolio_ids=[world.live])[0].status == "approved"


class _Blind:
    """A trader that cannot look one order up (the broker times out on it)."""

    def __init__(self, inner, blind: str) -> None:
        self._inner, self._blind = inner, blind

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_order_state(self, client_id):
        if client_id == self._blind:
            raise TimeoutError("no answer")
        return self._inner.get_order_state(client_id)


def _unknown_order(world, client_id: str) -> None:
    world.state.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status, state,"
        " created_at, updated_at, portfolio_id) VALUES (?, 'FLAT.US', 'buy', 1, 'market',"
        " 'pending', 'unknown', 'x', 'x', ?)",
        [client_id, world.live],
    )


def test_an_unknown_order_shuts_the_submit_window(world):
    settings = replace(modes.SETTINGS, live=LiveSettings(submit_in_window=True))
    _tick(world, DAY1, settings)
    _unknown_order(world, "old-order")

    def blind(account):
        return _Blind(world.traders(account), "old-order")

    result = _submit(world, IN_WINDOW, traders=blind)
    assert result.sent == 0 and result.portfolios[0].status == "skipped"
    assert "unknown" in (result.portfolios[0].reason or "")
    assert world.book.orders == {}
    # reconciliation resolves it (the broker never saw it), then the window opens
    assert _submit(world, IN_WINDOW + timedelta(minutes=1)).sent == 1


def test_the_tick_waits_for_unknown_orders_before_deciding(world):
    _unknown_order(world, "old-order")

    def blind(account):
        return _Blind(world.traders(account), "old-order")

    result = _tick(world, DAY1, traders=blind)
    [live] = [r for r in result.portfolios if r.portfolio_id == world.live]
    assert live.status == "noop" and live.summary["reason"] == "orders_unreconciled"
    assert live.summary["unknown_orders"] == ["old-order"]
    assert world.book.orders == {}


# ---- runaway -----------------------------------------------------------------------------


def test_a_runaway_halt_holds_even_an_auto_books_closes(world):
    _tick(world, DAY1)  # auto, sent at once: UP.US bought
    assert world.book.orders
    world.registry.set_status("bh_up", "retired", actor="t", reason="gone")
    world.state.execute("UPDATE subscriptions SET paused_reason = NULL WHERE id = ?",
                        [world.bob_auto])  # fmt: skip
    trip_halt(world.state, "runaway", reason="too many closes", actor="system",
              portfolio_id=world.live)  # fmt: skip

    _tick(world, DAY2)
    [sell] = [t for t in list_tickets(world.state, portfolio_ids=[world.live]) if t.side == "sell"]
    assert (sell.ticker, sell.hold, sell.status) == ("UP.US", "runaway", "awaiting_approval")
    assert [cid for cid in world.book.orders if "sell" in cid] == []


# ---- the fine order state -------------------------------------------------------------


def test_the_ticks_order_rows_carry_the_fine_state(world):
    _tick(world, DAY1)
    [paper] = world.orders(world.sim)
    assert _live_row(world, paper["client_id"]) == ("filled", "filled")
    [auto] = world.orders(world.live)
    assert _live_row(world, auto["client_id"]) == ("filled", "filled")


def test_a_submit_with_no_answer_is_unknown_until_reconciled(world):
    world.book.fail_orders = TimeoutError("no answer")
    _tick(world, DAY1)
    [auto] = world.orders(world.live)
    assert _live_row(world, auto["client_id"]) == ("pending", "unknown")


def test_a_broker_rejection_fails_the_ticket(world):
    settings = replace(modes.SETTINGS, live=LiveSettings(submit_in_window=True))
    _tick(world, DAY1, settings)
    world.book.quotes.pop("UP.US")  # the fake rejects an order with no quote
    result = _submit(world, IN_WINDOW)
    assert result.sent == 0 and result.portfolios[0].failed == 1
    [ticket] = list_tickets(world.state, portfolio_ids=[world.live])
    assert ticket.status == "failed" and "no quote" in (ticket.status_reason or "")
    assert _live_row(world, ticket.client_id) == ("rejected", "rejected")


def test_a_provider_outage_at_submit_leaves_the_tickets(world):
    settings = replace(modes.SETTINGS, live=LiveSettings(submit_in_window=True))
    _tick(world, DAY1, settings)
    world.book.fail = ProviderError("down", status=503)
    result = _submit(world, IN_WINDOW)
    assert result.portfolios[0].status == "error"
    assert list_tickets(world.state, portfolio_ids=[world.live])[0].status == "approved"
