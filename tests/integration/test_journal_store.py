"""Migration 050 and the journal store (roadmap 23.3): playbooks, trade
annotations and labels, and the ledger read."""

from __future__ import annotations

import sqlite3

import pytest

from stonks.journal.store import (
    JournalStoreError,
    create_playbook,
    list_playbooks,
    load_annotations,
    normalize_labels,
    save_annotation,
    update_playbook,
    used_labels,
)
from stonks.journal.trips import load_ledger
from stonks.store.state import SqliteState

NOW = "2026-03-17T20:00:00+00:00"
PF = "pf_default"
OWNER = "usr_owner"


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


def _order_and_fill(
    state,
    cid,
    side="buy",
    *,
    portfolio=PF,
    origin="strategy",
    context=None,
    order_type="market",
    stop_price=None,
) -> int:
    state.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status, created_at,"
        " updated_at, portfolio_id, origin, decision_context_json, stop_price)"
        " VALUES (?, 'X.US', ?, 10, ?, 'filled', ?, ?, ?, ?, ?, ?)",
        [cid, side, order_type, NOW, NOW, portfolio, origin, context, stop_price],
    )
    cur = state.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at, portfolio_id)"
        " VALUES (?, 'X.US', 10, 100.0, 1.0, ?, ?)",
        [cid, NOW, portfolio],
    )
    return int(cur.lastrowid or 0)


def test_annotation_round_trip_replaces_labels(state):
    fid = _order_and_fill(state, "b1")
    pb = create_playbook(state, OWNER, name="  Breakout  ", description="buy the high")
    save_annotation(
        state,
        PF,
        fid,
        playbook_id=pb.id,
        followed_plan=True,
        review=" late ",
        tags=["Gap Up", "gap  up", "earnings"],
        mistakes=["chased"],
        actor="user:x",
    )
    [ann] = load_annotations(state, PF).values()
    assert ann.trade_id == fid
    assert (ann.playbook_id, ann.followed_plan, ann.review) == (pb.id, True, "late")
    assert ann.tags == ("earnings", "gap up")
    assert ann.mistakes == ("chased",)
    save_annotation(
        state,
        PF,
        fid,
        playbook_id=None,
        followed_plan=None,
        review=None,
        tags=[],
        mistakes=["sized too big"],
        actor="user:x",
    )
    [ann] = load_annotations(state, PF).values()
    assert (ann.playbook_id, ann.followed_plan, ann.tags) == (None, None, ())
    assert used_labels(state, PF) == {"tag": [], "mistake": ["sized too big"]}


def test_an_annotation_must_be_on_a_fill_of_its_portfolio(state):
    fid = _order_and_fill(state, "b1")
    with pytest.raises(sqlite3.IntegrityError):
        save_annotation(
            state,
            "pf_other",
            fid,
            playbook_id=None,
            followed_plan=None,
            review=None,
            tags=[],
            mistakes=[],
            actor="user:x",
        )


def test_labels_are_checked():
    with pytest.raises(JournalStoreError):
        normalize_labels(["x" * 41])
    with pytest.raises(JournalStoreError):
        normalize_labels([f"t{i}" for i in range(21)])
    assert normalize_labels(["  A  b ", "", "a B"]) == ("a b",)


def test_playbooks_are_per_owner_named_once_and_archivable(state):
    pb = create_playbook(state, OWNER, name="Pullback", description=None)
    with pytest.raises(JournalStoreError):
        create_playbook(state, OWNER, name="Pullback", description="again")
    renamed = update_playbook(state, pb, name="Pullback to MA", description="rules")
    assert (renamed.name, renamed.description) == ("Pullback to MA", "rules")
    update_playbook(state, renamed, archived=True)
    assert list_playbooks(state, OWNER) == []
    assert [p.name for p in list_playbooks(state, OWNER, include_archived=True)] == [
        "Pullback to MA"
    ]


def test_load_ledger_reads_one_portfolios_fills_orders_and_stops(state):
    b = _order_and_fill(state, "b1", context='{"trigger": "signal"}')
    _order_and_fill(state, "m1", "sell", origin="manual")
    state.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status, created_at,"
        " updated_at, portfolio_id, stop_price, protective) VALUES"
        " ('b1:stop', 'X.US', 'sell', 10, 'stop', 'pending', ?, ?, ?, 95.0, 1)",
        [NOW, NOW, PF],
    )
    ledger = load_ledger(state, PF)
    assert ledger.fills[0].fill_id == b
    assert [f.origin for f in ledger.fills] == ["strategy", "manual"]
    assert ledger.orders["b1"].context == {"trigger": "signal"}
    assert [o.client_id for o in ledger.stop_orders] == ["b1:stop"]
    assert load_ledger(state, "pf_other").fills == ()
