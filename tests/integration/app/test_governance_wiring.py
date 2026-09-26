"""Integration 1 (W1.6): the REST routes, Studio and MCP tools pass
reason / override / actor through the governed ``change_status``."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from stonks.api import create_app
from tests.integration.app.test_api import AUTH, LOOPBACK

LONG_REASON = "owner override: incubation cut short, see ticket 42"


@pytest.fixture
def client(settings, seeded, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    app = create_app(settings, source_factory=lambda: fake_source)
    with TestClient(app, client=LOOPBACK) as c:
        yield c


def test_promote_route_takes_override_and_reason_and_audits_the_caller(client, seeded):
    sid = seeded["shadow_id"]
    resp = client.post(
        f"/api/strategies/{sid}/promote",
        json={"reason": LONG_REASON, "override": True, "actor": "ui:alice"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["status"] == "active"
    change = body["status_history"][-1]
    assert change["override"] is True
    assert change["reason"] == LONG_REASON
    # The body's actor is ignored: the audit row names the caller.
    assert change["actor"] == "user:usr_owner"


def test_promote_route_short_override_reason_is_422(client, seeded):
    resp = client.post(
        f"/api/strategies/{seeded['shadow_id']}/promote",
        json={"reason": "short", "override": True},
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert "20" in resp.json()["detail"]


def test_refused_promotion_is_409_with_failing_checks(client, seeded):
    resp = client.post(f"/api/strategies/{seeded['shadow_id']}/promote", json={}, headers=AUTH)
    assert resp.status_code == 409
    assert "min_days" in resp.json()["detail"]


@pytest.mark.parametrize(("action", "status"), [("retire", "retired"), ("shadow", "shadow")])
def test_demotion_routes_take_a_reason(client, seeded, action, status):
    sid = seeded["active_id"]
    resp = client.post(
        f"/api/strategies/{sid}/{action}", json={"reason": "edge decayed"}, headers=AUTH
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["status"] == status
    assert resp.json()["status_history"][-1]["actor"] == "user:usr_owner"


def test_history_route(client, seeded):
    sid = seeded["active_id"]
    history = client.get(f"/api/strategies/{sid}/history").json()["items"]
    assert [(c["from_status"], c["to_status"]) for c in history] == [("shadow", "active")]
    client.post(f"/api/strategies/{sid}/retire", json={"reason": "done"}, headers=AUTH)
    history = client.get(f"/api/strategies/{sid}/history").json()["items"]
    assert history[-1]["to_status"] == "retired"
    assert client.get("/api/strategies/missing/history").status_code == 404


def test_actor_is_bounded(client, seeded):
    resp = client.post(
        f"/api/strategies/{seeded['active_id']}/retire",
        json={"reason": "done", "actor": "x" * 500},
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_strategy_list_search(client, seeded):
    page = client.get("/api/strategies", params={"q": "ACTIVE"}).json()
    assert [s["id"] for s in page["items"]] == [seeded["active_id"]]
    assert page["total"] == 1
    # matches the class path too
    page = client.get("/api/strategies", params={"q": "buy_and_hold"}).json()
    assert page["total"] == 2
    page = client.get("/api/strategies", params={"q": "zzz"}).json()
    assert page["items"] == [] and page["total"] == 0


def test_studio_enable_disable_take_reason_and_override(client):
    draft = client.post(
        "/api/studio/drafts",
        json={"name": "gov", "kind": "rule", "spec": _spec()},
        headers=AUTH,
    ).json()
    client.post(f"/api/studio/drafts/{draft['id']}/register", headers=AUTH)
    refused = client.post(f"/api/studio/drafts/{draft['id']}/enable", headers=AUTH)
    assert refused.status_code == 409
    on = client.post(
        f"/api/studio/drafts/{draft['id']}/enable",
        json={"reason": LONG_REASON, "override": True},
        headers=AUTH,
    )
    assert on.status_code == 200, on.json()
    assert on.json()["strategy_status"] == "active"
    off = client.post(
        f"/api/studio/drafts/{draft['id']}/disable",
        json={"reason": "back to paper"},
        headers=AUTH,
    )
    assert off.status_code == 200, off.json()
    assert off.json()["strategy_status"] == "shadow"
    sid = off.json()["registered_strategy_id"]
    history = client.get(f"/api/strategies/{sid}/history").json()["items"]
    assert [c["actor"] for c in history] == ["user:usr_owner", "user:usr_owner"]


def _spec():
    from stonks.strategies.rules import TEMPLATES

    return next(iter(TEMPLATES.values())).spec
