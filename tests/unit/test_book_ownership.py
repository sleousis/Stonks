"""BE-02: an auto book trades only what it owns in a connected account.

Ownership comes from the book's own fill ledger (net filled quantity per
ticker in that portfolio, split-adjusted). Holdings the book never bought
are the user's own: marked, never traded."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.corporate_actions import CorporateActions, Split
from stonks.core.types import Order, Portfolio
from stonks.production.ownership import (
    drop_unowned_crossings,
    managed_view,
    manual_positions,
    merge_holdings,
    owned_positions,
    strip_holdings,
)
from stonks.store.state import SqliteState


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    for pid in ("pf_b", "pf_other"):
        s.execute(
            "INSERT INTO portfolios (id, owner_id, name, kind, created_at)"
            " VALUES (?, 'usr_owner', ?, 'broker', '2026-01-01')",
            [pid, pid],
        )
    yield s
    s.close()


def _fill(state, cid, ticker, side, qty, day, portfolio_id="pf_b"):
    state.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status, portfolio_id,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, 'market', 'filled', ?, ?, ?)",
        [cid, ticker, side, qty, portfolio_id, day, day],
    )
    state.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at, portfolio_id)"
        " VALUES (?, ?, ?, 10.0, 0.0, ?, ?)",
        [cid, ticker, qty, f"{day}T15:00:00+00:00", portfolio_id],
    )


def test_owned_positions_nets_the_books_fills_in_its_portfolio_only(state):
    _fill(state, "a", "UP.US", "buy", 10, "2026-03-02")
    _fill(state, "b", "UP.US", "sell", 4, "2026-03-03")
    _fill(state, "c", "DN.US", "sell", 3, "2026-03-03")  # a short
    _fill(state, "d", "UP.US", "buy", 99, "2026-03-03", portfolio_id="pf_other")
    assert owned_positions(state, "pf_b") == {"UP.US": 6.0, "DN.US": -3.0}


def test_owned_positions_follow_splits_after_the_fill(state):
    _fill(state, "a", "UP.US", "buy", 10, "2026-03-02")
    _fill(state, "b", "UP.US", "buy", 1, "2026-03-10")
    actions = CorporateActions.from_events([Split("UP.US", date(2026, 3, 5), 2.0)])
    assert owned_positions(state, "pf_b", actions) == {"UP.US": 21.0}


def test_a_flat_ticker_is_not_owned(state):
    _fill(state, "a", "UP.US", "buy", 10, "2026-03-02")
    _fill(state, "b", "UP.US", "sell", 10, "2026-03-03")
    assert owned_positions(state, "pf_b") == {}


def test_managed_view_keeps_only_the_owned_part_of_each_holding():
    account = Portfolio(cash=1000.0, positions={"FLAT.US": 10.0, "UP.US": 8.0, "DN.US": -2.0})
    managed, external = managed_view(account, {"UP.US": 5.0, "DN.US": 4.0, "GONE.US": 3.0})
    assert managed.cash == 1000.0
    # owned more than the account holds: clipped; a sign mismatch owns nothing
    assert managed.positions == {"UP.US": 5.0}
    assert external == {"FLAT.US": 10.0, "UP.US": 3.0, "DN.US": -2.0}


def test_orders_that_would_trade_the_users_own_holding_are_dropped():
    managed = {"UP.US": 5.0}
    external = {"UP.US": 3.0, "FLAT.US": 10.0, "DN.US": -2.0}
    orders = [
        Order(client_id="1", ticker="UP.US", side="sell", quantity=5.0),  # own part: kept
        Order(client_id="2", ticker="FLAT.US", side="buy", quantity=1.0),  # same side: kept
        Order(client_id="3", ticker="FLAT.US", side="sell", quantity=2.0),  # would sell user's
        Order(client_id="4", ticker="DN.US", side="buy", quantity=1.0),  # would cover user's
        Order(client_id="5", ticker="NEW.US", side="sell", quantity=1.0),  # no user holding
    ]
    kept, dropped = drop_unowned_crossings(orders, managed, external)
    assert [o.client_id for o in kept] == ["1", "2", "5"]
    assert dropped == ["DN.US", "FLAT.US"]


# ---- manual orders (roadmap 20.1) --------------------------------------------------


def _manual(state, cid, ticker, side, qty, day, portfolio_id="pf_b"):
    _fill(state, cid, ticker, side, qty, day, portfolio_id)
    state.execute("UPDATE orders SET origin = 'manual' WHERE client_id = ?", [cid])


def test_manual_fills_are_not_owned_by_the_book(state):
    _fill(state, "a", "UP.US", "buy", 10, "2026-03-02")
    _manual(state, "m", "UP.US", "buy", 4, "2026-03-03")
    assert owned_positions(state, "pf_b") == {"UP.US": 10.0}


def test_manual_positions_net_only_manual_fills(state):
    _fill(state, "a", "UP.US", "buy", 10, "2026-03-02")
    _manual(state, "m1", "UP.US", "buy", 4, "2026-03-03")
    _manual(state, "m2", "FLAT.US", "buy", 5, "2026-03-03")
    _manual(state, "m3", "FLAT.US", "sell", 5, "2026-03-04")
    _manual(state, "m4", "DN.US", "buy", 2, "2026-03-03", portfolio_id="pf_other")
    assert manual_positions(state, "pf_b") == {"UP.US": 4.0}


def test_without_manual_holdings_the_view_keeps_everything():
    account = Portfolio(cash=5.0, positions={"UP.US": 3.0})
    managed, external = strip_holdings(account, {})
    assert managed.positions == {"UP.US": 3.0} and external == {}


def test_strip_holdings_takes_the_manual_part_out():
    account = Portfolio(cash=100.0, positions={"UP.US": 10.0, "FLAT.US": 5.0, "DN.US": -2.0})
    managed, external = strip_holdings(account, {"UP.US": 4.0, "FLAT.US": 9.0, "DN.US": 1.0})
    assert managed.cash == 100.0
    assert managed.positions == {"UP.US": 6.0, "DN.US": -2.0}
    assert external == {"UP.US": 4.0, "FLAT.US": 5.0}


def test_merge_holdings_puts_them_back():
    managed = Portfolio(cash=10.0, positions={"UP.US": 6.0})
    merged = merge_holdings(managed, {"UP.US": 4.0, "FLAT.US": 5.0})
    assert merged.cash == 10.0
    assert merged.positions == {"UP.US": 10.0, "FLAT.US": 5.0}
