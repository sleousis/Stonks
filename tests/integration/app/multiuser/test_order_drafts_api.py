"""Order drafts through the REST API (roadmap 20.4 safety): a proposal is
priced by the server, approved only with a fresh second factor, placed
through every check, private, and cancelled by the kill switch. The
assistant may draft only inside its envelope."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from stonks.accounts import PortfolioRepository, Role, Scope
from stonks.app.manual_orders import ManualOrdersService
from stonks.app.order_drafts import OrderDraftCreate, OrderDraftService
from stonks.auth.errors import PermissionDenied
from stonks.auth.principal import ROLE_SCOPES, Principal
from stonks.store.state import SqliteState
from tests.integration.app.stepup import allow_step_up

NOW = datetime(2026, 4, 2, 15, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clock(app):
    services = app.state.services
    manual = ManualOrdersService(services.context, clock=lambda: NOW)
    services.manual_orders = manual
    services.order_drafts = OrderDraftService(services.context, manual, clock=lambda: NOW)


def _book(settings, user_id: str) -> str:
    with SqliteState(settings.state.path) as state:
        return (
            PortfolioRepository(state)
            .create(Scope(user_id=user_id, role=Role.TRADER), name="Book", initial_cash=10_000.0)
            .id
        )


def _body(pid: str, **kw):
    return {
        "portfolio_id": pid,
        "ticker": "UP.US",
        "side": "buy",
        "quantity": 5,
        "reason": "worth a look",
        "retry_key": "r1",
    } | kw


def test_a_draft_is_priced_and_approved_only_with_a_fresh_second_factor(
    app, client, people, settings
):
    alice = people["alice"]
    pid = _book(settings, alice["id"])
    made = client.post("/api/orders/drafts", json=_body(pid), headers=alice["headers"])
    assert made.status_code == 201, made.text
    draft = made.json()
    assert draft["status"] == "pending" and draft["source"] == "mcp"
    assert draft["reference_price"] == pytest.approx(199.5, abs=1.0)
    assert draft["notional"] == pytest.approx(5 * draft["reference_price"])
    again = client.post("/api/orders/drafts", json=_body(pid), headers=alice["headers"])
    assert again.json()["id"] == draft["id"]
    # a token never approves
    denied = client.post(f"/api/orders/drafts/{draft['id']}/approve", headers=alice["headers"])
    assert denied.status_code == 403 and denied.json()["code"] == "step_up_required"
    allow_step_up(app)
    ok = client.post(f"/api/orders/drafts/{draft['id']}/approve", headers=alice["headers"])
    assert ok.status_code == 200, ok.text
    out = ok.json()
    assert out["draft"]["status"] == "placed" and out["order"]["status"] == "filled"
    assert out["draft"]["client_id"] == out["order"]["client_id"] == f"manual:{pid}:{draft['id']}"
    twice = client.post(f"/api/orders/drafts/{draft['id']}/approve", headers=alice["headers"])
    assert twice.status_code == 409


def test_a_refused_placement_leaves_the_draft_rejected(app, client, people, settings):
    alice = people["alice"]
    pid = _book(settings, alice["id"])
    draft = client.post(
        "/api/orders/drafts", json=_body(pid, quantity=1000), headers=alice["headers"]
    ).json()
    allow_step_up(app)
    resp = client.post(f"/api/orders/drafts/{draft['id']}/approve", headers=alice["headers"])
    assert resp.status_code == 409
    listed = client.get("/api/orders/drafts", headers=alice["headers"]).json()["items"]
    assert listed[0]["status"] == "rejected" and "not placed" in listed[0]["decision_note"]


def test_drafts_are_private_and_checked(client, people, settings):
    alice, bob = people["alice"], people["bob"]
    pid = _book(settings, alice["id"])
    assert (
        client.post("/api/orders/drafts", json=_body(pid), headers=bob["headers"]).status_code
        == 404
    )
    draft = client.post("/api/orders/drafts", json=_body(pid), headers=alice["headers"]).json()
    assert client.get("/api/orders/drafts", headers=bob["headers"]).json()["total"] == 0
    rej = client.post(f"/api/orders/drafts/{draft['id']}/reject", json={}, headers=bob["headers"])
    assert rej.status_code == 404
    band = client.post(
        "/api/orders/drafts",
        json=_body(pid, retry_key="r2", order_type="limit", limit_price=1.0),
        headers=alice["headers"],
    )
    assert band.status_code == 409 and "band" in band.json()["detail"]
    vic = client.post("/api/orders/drafts", json=_body(pid), headers=people["vic"]["headers"])
    assert vic.status_code == 403


def test_the_kill_switch_cancels_pending_drafts(client, people, settings):
    alice = people["alice"]
    pid = _book(settings, alice["id"])
    client.post("/api/orders/drafts", json=_body(pid), headers=alice["headers"])
    kill = client.post(
        "/api/halts/kill", json={"scope": "user", "reason": "stop"}, headers=alice["headers"]
    )
    assert kill.status_code in (200, 201)
    listed = client.get("/api/orders/drafts", headers=alice["headers"]).json()["items"]
    assert [d["status"] for d in listed] == ["cancelled"]


def _assistant(alice) -> Principal:
    base = Principal.create(
        user_id=alice["id"],
        kind="human",
        role=Role.TRADER,
        scopes=ROLE_SCOPES[Role.TRADER],
        mfa_fresh=True,
        via="session",
    )
    return dataclasses.replace(base, via="assistant", mfa_fresh=False)


def test_the_assistant_drafts_only_inside_its_envelope(app, people, settings):
    alice = people["alice"]
    pid = _book(settings, alice["id"])
    services = app.state.services
    principal = _assistant(alice)
    body = OrderDraftCreate(**_body(pid))
    with pytest.raises(PermissionDenied, match="research only"):
        services.order_drafts.create(principal, body)
    env = services.context.settings.assistant.envelope
    env.order_tools = True
    env.max_order_notional = 100.0
    from stonks.app.errors import ConflictError

    with pytest.raises(ConflictError, match="per-order cap"):
        services.order_drafts.create(principal, body)
    env.max_order_notional = None
    made = services.order_drafts.create(principal, body, conversation_id="cnv_x")
    assert made.source == "assistant" and made.conversation_id == "cnv_x"
    # the assistant can never approve
    with pytest.raises(PermissionDenied):
        services.order_drafts.approve(principal, made.id)
