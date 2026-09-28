"""Order drafts (roadmap 20.4 safety): the server checks and prices every
draft, the envelope caps it, a retry key never makes two, and the kill
switch cancels pending ones."""

from __future__ import annotations

from datetime import timedelta

import pytest

from stonks.production.order_drafts import (
    DraftNotFound,
    DraftRefused,
    DraftRequest,
    Envelope,
    cancel_drafts,
    claim_draft,
    create_draft,
    get_draft,
    list_drafts,
)
from tests.unit import test_manual_orders as _shared

NOW = _shared.NOW
# The tmp lake, state, trader and portfolio fixtures of the manual order tests.
lake = _shared.lake
state = _shared.state
owner = _shared.owner
portfolio_id = _shared.portfolio_id


def _req(owner: str, pid: str, **kw) -> DraftRequest:
    base = {
        "owner_id": owner,
        "portfolio_id": pid,
        "source": "assistant",
        "retry_key": "k1",
        "ticker": "UP.US",
        "side": "buy",
        "quantity": 10.0,
        "reason": "the model's idea",
    }
    return DraftRequest(**(base | kw))


def test_the_server_prices_the_draft(state, lake, owner, portfolio_id):
    draft, created = create_draft(state, lake, _req(owner, portfolio_id), Envelope(), now=NOW)
    assert created and draft.status == "pending"
    assert draft.reference_price == 100.0 and draft.notional == 1000.0
    assert draft.expires_at == (NOW + timedelta(hours=24)).isoformat(timespec="seconds")


def test_the_same_retry_key_returns_the_same_draft(state, lake, owner, portfolio_id):
    first, _ = create_draft(state, lake, _req(owner, portfolio_id), Envelope(), now=NOW)
    again, created = create_draft(
        state, lake, _req(owner, portfolio_id, quantity=99.0), Envelope(), now=NOW
    )
    assert not created and again.id == first.id and again.quantity == 10.0


@pytest.mark.parametrize(
    ("kw", "envelope", "match"),
    [
        ({"ticker": "NOPE.US"}, Envelope(), "no recent close"),
        ({}, Envelope(allowed_tickers=frozenset({"FLAT.US"})), "allowed list"),
        ({}, Envelope(max_order_notional=500.0), "per-order cap"),
        ({"order_type": "limit", "limit_price": 120.0}, Envelope(), "outside the 5% band"),
        ({"order_type": "limit"}, Envelope(), "positive limit"),
        ({"limit_price": 100.0}, Envelope(), "market order takes no limit"),
        ({"reason": "x"}, Envelope(), "say why"),
    ],
)
def test_checks_refuse_and_create_nothing(state, lake, owner, portfolio_id, kw, envelope, match):
    with pytest.raises(DraftRefused, match=match):
        create_draft(state, lake, _req(owner, portfolio_id, **kw), envelope, now=NOW)
    assert state.sql("SELECT COUNT(*) AS n FROM order_drafts")[0]["n"] == 0


def test_a_limit_inside_the_band_is_fine(state, lake, owner, portfolio_id):
    draft, _ = create_draft(
        state,
        lake,
        _req(owner, portfolio_id, order_type="limit", limit_price=98.0),
        Envelope(),
        now=NOW,
    )
    assert draft.limit_price == 98.0


def test_the_daily_cap_counts_todays_drafts(state, lake, owner, portfolio_id):
    env = Envelope(max_day_notional=1500.0)
    create_draft(state, lake, _req(owner, portfolio_id), env, now=NOW)
    with pytest.raises(DraftRefused, match="daily cap"):
        create_draft(state, lake, _req(owner, portfolio_id, retry_key="k2"), env, now=NOW)
    # a rejected draft frees its share of the cap
    first = list_drafts(state, owner, now=NOW)[0]
    claim_draft(state, owner, first.id, "rejected", actor="user:x", now=NOW)
    create_draft(state, lake, _req(owner, portfolio_id, retry_key="k3"), env, now=NOW)


def test_claim_happens_once_and_expired_drafts_cannot_be_claimed(state, lake, owner, portfolio_id):
    env = Envelope(ttl=timedelta(minutes=5))
    draft, _ = create_draft(state, lake, _req(owner, portfolio_id), env, now=NOW)
    later = NOW + timedelta(minutes=10)
    with pytest.raises(DraftRefused, match="expired"):
        claim_draft(state, owner, draft.id, "placed", actor="user:x", now=later)
    other, _ = create_draft(state, lake, _req(owner, portfolio_id, retry_key="k2"), env, now=NOW)
    claim_draft(state, owner, other.id, "placed", actor="user:x", now=NOW)
    with pytest.raises(DraftRefused, match="placed"):
        claim_draft(state, owner, other.id, "rejected", actor="user:x", now=NOW)


def test_drafts_are_private(state, lake, owner, portfolio_id):
    draft, _ = create_draft(state, lake, _req(owner, portfolio_id), Envelope(), now=NOW)
    with pytest.raises(DraftNotFound):
        get_draft(state, "usr_someone_else", draft.id)
    assert list_drafts(state, "usr_someone_else", now=NOW) == []


def test_cancel_drafts_by_person_portfolio_or_everyone(state, lake, owner, portfolio_id):
    for key in ("a", "b"):
        create_draft(state, lake, _req(owner, portfolio_id, retry_key=key), Envelope(), now=NOW)
    assert cancel_drafts(state, reason="kill", portfolio_ids=["pf_other"]) == 0
    assert cancel_drafts(state, reason="kill") == 0  # no target: nothing
    assert cancel_drafts(state, reason="kill", user_id=owner) == 2
    assert {d.status for d in list_drafts(state, owner, now=NOW)} == {"cancelled"}
    create_draft(state, lake, _req(owner, portfolio_id, retry_key="c"), Envelope(), now=NOW)
    assert cancel_drafts(state, reason="global kill", everyone=True) == 1
