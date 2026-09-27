"""The live dry-run preview (roadmap 19.9): it decides like the tick, reads
the broker, asks its what-if, and never transmits."""

from __future__ import annotations

import pytest

import tests.integration.test_tick_modes as modes
from stonks.core.types import Order, Portfolio
from stonks.execution.brokers.base import MarginPreview
from stonks.production.live.preview import (
    PreviewBroker,
    PreviewError,
    TransmitRefusedError,
    run_preview,
)
from stonks.production.tick import load_tick_plan

DAY1 = modes.DAY1


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


class _WhatIfBroker:
    """A broker with a what-if that also records any write."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.writes: list[str] = []
        self.closed = False

    def fetch_portfolio(self) -> Portfolio:
        return Portfolio(cash=1_000.0)

    def place_order(self, order):
        self.writes.append(order.client_id)

    def reconcile(self):
        return []

    def cancel_order(self, client_id):
        self.writes.append(client_id)
        return True

    def what_if(self, order: Order) -> MarginPreview:
        if self.fail:
            raise TimeoutError("what-if timed out")
        return MarginPreview(
            client_id=order.client_id or "",
            initial_margin_change=0.0,
            maintenance_margin_change=0.0,
            equity_with_loan_after=900.0,
            commission=1.0,
            commission_currency="USD",
        )

    def close(self):
        self.closed = True


def test_the_preview_broker_never_writes():
    inner = _WhatIfBroker()
    broker = PreviewBroker(inner)
    order = Order(client_id="c1", ticker="A.US", side="buy", quantity=1.0)
    with pytest.raises(TransmitRefusedError):
        broker.place_order(order)
    with pytest.raises(TransmitRefusedError):
        broker.cancel_order("c1")
    with pytest.raises(TransmitRefusedError):
        broker.cancel_all()
    assert inner.writes == []
    assert broker.what_if(order).commission == 1.0
    assert broker.fetch_portfolio().cash == 1_000.0
    assert broker.reconcile() == []
    broker.close()
    assert inner.closed


def test_a_preview_decides_like_the_tick_and_sends_nothing(world):
    plan = load_tick_plan(world.state, modes.SETTINGS, traders=world.traders, dry_run=True)
    preview = run_preview(
        world.state, world.lake, world.registry, modes.SETTINGS, plan, world.live, as_of=DAY1
    )
    assert preview.portfolio_id == world.live and preview.transmitted is False
    assert preview.stage == "sim_paper"
    [order] = preview.orders
    assert (order.ticker, order.side, order.strategy_id) == ("UP.US", "buy", "bh_up")
    assert order.notional is not None and order.notional > 0
    # the fake connection has no what-if: said so, nothing guessed
    assert order.what_if is None and not preview.what_if_available
    assert any("what-if" in n for n in preview.notes)
    # nothing reached the broker, nothing was booked
    assert world.book.orders == {} and "place_order" not in world.book.calls
    assert world.orders(world.live) == []
    assert world.state.sql("SELECT COUNT(*) AS n FROM order_tickets")[0]["n"] == 0


def test_a_preview_asks_the_brokers_what_if(world):
    inner = _WhatIfBroker()
    plan = load_tick_plan(world.state, modes.SETTINGS, traders=lambda account: inner, dry_run=True)
    preview = run_preview(
        world.state, world.lake, world.registry, modes.SETTINGS, plan, world.live, as_of=DAY1
    )
    assert preview.what_if_available
    assert all(o.what_if is not None and o.what_if.commission == 1.0 for o in preview.orders)
    assert inner.writes == [] and inner.closed


def test_a_failed_what_if_is_reported_not_hidden(world):
    inner = _WhatIfBroker(fail=True)
    plan = load_tick_plan(world.state, modes.SETTINGS, traders=lambda account: inner, dry_run=True)
    preview = run_preview(
        world.state, world.lake, world.registry, modes.SETTINGS, plan, world.live, as_of=DAY1
    )
    assert preview.orders and all(o.what_if is None for o in preview.orders)
    assert all("timed out" in (o.what_if_error or "") for o in preview.orders)


def test_a_portfolio_without_a_live_book_cannot_be_previewed(world):
    plan = load_tick_plan(world.state, modes.SETTINGS, traders=world.traders, dry_run=True)
    with pytest.raises(PreviewError, match="no live book"):
        run_preview(
            world.state, world.lake, world.registry, modes.SETTINGS, plan, world.sim, as_of=DAY1
        )
