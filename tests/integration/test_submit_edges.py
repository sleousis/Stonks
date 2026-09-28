"""Edges of the submit window (roadmap 19.8): a broker that cannot look
orders up, a gate that fails, a halt of buys, quotes that fail, an alert
that cannot be sent, a submit with no answer, a sync that fails after the
send, and an order that was sent before a crash."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import tests.integration.test_tick_modes as modes
from stonks.accounts import PortfolioRepository, Scope
from stonks.core.clock import FakeClock
from stonks.execution.order_state import current_state, write_state
from stonks.production.halts import trip_halt
from stonks.production.submit import submit_tickets
from stonks.production.tick import _record_order
from stonks.production.tickets import list_tickets
from tests.integration.test_submit_gate import BAND, WINDOWED, _decision, _Quoting
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
    _tick(w, DAY1, WINDOWED)  # one approved ticket for UP.US in the live book
    yield w
    w.state.close()


def trader(world):
    account = PortfolioRepository(world.state).get(Scope.service("t"), world.live)
    return world.traders(account)


def submit(world, wrap=lambda t: t, **kw):
    return submit_tickets(world.state, lambda _pid: wrap(trader(world)),
                          clock=FakeClock(IN_WINDOW), **kw)  # fmt: skip


def ticket(world):
    [t] = list_tickets(world.state, portfolio_ids=[world.live])
    return t


class NoLookup:
    """Places orders but cannot look them up by client id."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def fetch_portfolio(self):
        return self._inner.fetch_portfolio()

    def place_order(self, order):
        return self._inner.place_order(order)

    def reconcile(self):
        return []


class Faulty(_Quoting):
    """A quoting trader whose calls can fail."""

    def __init__(self, inner, *, portfolio=None, place=None, lookup_after_send=None,
                 quotes_error=None, quotes=None) -> None:  # fmt: skip
        super().__init__(inner, quotes or {})
        self.portfolio_error = portfolio
        self.place_error = place
        self.lookup_error = lookup_after_send
        self.quotes_error = quotes_error
        self.sent = False

    def fetch_portfolio(self):
        if self.portfolio_error is not None:
            raise self.portfolio_error
        return super().fetch_portfolio()

    def place_order(self, order):
        if self.place_error is not None:
            raise self.place_error
        self.sent = True
        return super().place_order(order)

    def get_order_state(self, client_id):
        if self.sent and self.lookup_error is not None:
            raise self.lookup_error
        return super().get_order_state(client_id)

    def quotes(self, tickers):
        if self.quotes_error is not None:
            raise self.quotes_error
        return super().quotes(tickers)


def test_a_broker_that_cannot_look_orders_up_sends_nothing(world):
    result = submit(world, NoLookup)
    [pf] = result.portfolios
    assert (pf.status, pf.reason) == ("error", "the broker cannot look orders up by client id")
    assert ticket(world).status == "approved" and world.book.orders == {}


def test_a_gate_that_fails_sends_nothing(world):
    result = submit(world, lambda t: Faulty(t, portfolio=RuntimeError("bad answer")))
    [pf] = result.portfolios
    assert pf.status == "error" and pf.reason == "RuntimeError: bad answer"
    assert ticket(world).status == "approved"


def test_a_halt_of_buys_holds_an_opening_ticket(world):
    trip_halt(world.state, "kill", reason="drill", actor="t", portfolio_id=world.live,
              halt="buys", on=DAY1)  # fmt: skip
    result = submit(world)
    [pf] = result.portfolios
    assert (pf.sent, pf.held, pf.status) == (0, 1, "ok")
    assert ticket(world).status == "approved" and world.book.orders == {}


def test_quotes_that_fail_hold_the_opening_ticket(world):
    result = submit(world, lambda t: Faulty(t, quotes_error=TimeoutError("no data")), risk=BAND)
    [pf] = result.portfolios
    assert pf.sent == 0 and pf.held == 1


def test_an_alert_that_cannot_be_sent_still_holds_the_gap(world):
    def broken(_event):
        raise RuntimeError("push down")

    moved = {"UP.US": _decision(world) * 1.10}
    result = submit(world, lambda t: Faulty(t, quotes=moved), risk=BAND, publish=broken)
    [pf] = result.portfolios
    assert pf.gap_held == (ticket(world).id,) and pf.sent == 0


def test_a_submit_with_no_answer_is_unknown_and_never_sent_twice(world):
    result = submit(world, lambda t: Faulty(t, place=TimeoutError("gateway")))
    [pf] = result.portfolios
    assert (pf.status, pf.sent, pf.failed) == ("partial", 0, 1)
    t = ticket(world)
    assert t.status == "submitted" and t.status_reason == "outcome unknown"
    assert current_state(world.state, t.client_id) == "unknown"


def test_a_sync_that_fails_after_the_send_still_counts_the_send(world):
    result = submit(world, lambda t: Faulty(t, lookup_after_send=ConnectionError("dropped")))
    assert result.sent == 1
    assert current_state(world.state, ticket(world).client_id) == "submitted"


def test_an_order_sent_before_a_crash_is_followed_not_resent(world):
    t = ticket(world)
    broker = trader(world)
    _record_order(world.state, t.order, status="pending", portfolio_id=world.live)
    broker.place_order(t.order)
    write_state(world.state, t.client_id, "submitted")
    placed = dict(world.book.orders)
    result = submit(world)
    [pf] = result.portfolios
    assert (pf.sent, pf.failed) == (0, 0)
    assert ticket(world).status in ("submitted", "filled")
    assert world.book.orders.keys() == placed.keys()


def test_a_portfolio_gone_after_the_tick_sends_nothing(world):
    from stonks.production.submit import _book_running

    assert _book_running(world.state, world.live)
    assert not _book_running(world.state, "pf_missing")


def test_owner_limits_without_a_policy_keep_the_global_band(world):
    from stonks.production.submit import _price_band

    owner = {"owner_risk_json": None, "risk_policy_json": ""}
    assert _price_band(BAND, owner) == _price_band(BAND, None)
    assert _price_band(BAND, owner).max_gap_pct == 0.05


def test_the_gate_reason_names_material_kinds_and_detail():
    from stonks.execution.drift import DriftItem
    from stonks.production.live.checks import CheckResult, ReconcileReport
    from stonks.production.submit import _gate_reason

    report = ReconcileReport(
        id="rec_1", portfolio_id="pf", kind="submit",
        as_of=datetime(2026, 3, 18, tzinfo=UTC).date(), taken_at="x", status="fault",
        detail="LiveTradingRefusedError: wrong account",
    )  # fmt: skip
    assert _gate_reason(CheckResult(report)) == (
        "submit check rec_1 is fault (LiveTradingRefusedError: wrong account)"
    )
    drift = DriftItem(kind="position_qty", key="A.US", ours=1, broker=2, material=True)
    report = ReconcileReport(id="rec_2", portfolio_id="pf", kind="submit", as_of=report.as_of,
                             taken_at="x", status="drift", items=(drift,))  # fmt: skip
    assert _gate_reason(CheckResult(report)) == "submit check rec_2 is drift: position_qty"
