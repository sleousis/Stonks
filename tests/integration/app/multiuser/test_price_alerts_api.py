"""Price alerts through the REST API (roadmap 20.2): your own rules only,
roles, validation, the operator check and the feed it writes."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from stonks.app.price_alerts import PriceAlertService
from stonks.notify.router import NotificationRouter

NOW = datetime(2026, 4, 1, 22, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _service(app):
    services = app.state.services
    services.price_alerts = PriceAlertService(
        services.context,
        clock=lambda: NOW,
        publisher=lambda state: NotificationRouter(state, {}).publish,
    )


def test_create_list_update_delete_your_own_rule(client, people):
    alice = people["alice"]
    made = client.post(
        "/api/price-alerts",
        json={"condition": "crosses_above", "ticker": "up.us", "level": 150, "name": "breakout"},
        headers=alice["headers"],
    )
    assert made.status_code == 201, made.text
    rule = made.json()
    assert rule["ticker"] == "UP.US" and rule["target_kind"] == "ticker" and rule["enabled"]
    listed = client.get("/api/price-alerts", headers=alice["headers"]).json()
    assert [r["id"] for r in listed["items"]] == [rule["id"]]
    changed = client.patch(
        f"/api/price-alerts/{rule['id']}",
        json={"level": 160, "enabled": False},
        headers=alice["headers"],
    )
    assert changed.status_code == 200 and changed.json()["level"] == 160
    assert changed.json()["enabled"] is False
    gone = client.delete(f"/api/price-alerts/{rule['id']}", headers=alice["headers"])
    assert gone.status_code == 204
    assert client.get("/api/price-alerts", headers=alice["headers"]).json()["total"] == 0


def test_rules_are_private(client, people):
    alice, bob, ada = people["alice"], people["bob"], people["ada"]
    rid = client.post(
        "/api/price-alerts",
        json={"condition": "crosses_below", "ticker": "UP.US", "level": 50},
        headers=alice["headers"],
    ).json()["id"]
    for who in (bob, ada):
        assert client.get(f"/api/price-alerts/{rid}", headers=who["headers"]).status_code == 404
        assert client.delete(f"/api/price-alerts/{rid}", headers=who["headers"]).status_code == 404
        assert client.get("/api/price-alerts", headers=who["headers"]).json()["total"] == 0


def test_a_viewer_reads_but_cannot_create(client, people):
    vic = people["vic"]
    assert client.get("/api/price-alerts", headers=vic["headers"]).status_code == 200
    r = client.post(
        "/api/price-alerts",
        json={"condition": "crosses_above", "ticker": "UP.US", "level": 1},
        headers=vic["headers"],
    )
    assert r.status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {"condition": "crosses_above", "ticker": "UP.US"},  # no level
        {"condition": "moves_pct", "ticker": "UP.US", "pct": 5},  # no window
        {"condition": "crosses_above", "level": 1},  # no target
        {"condition": "crosses_above", "ticker": "UP.US", "watchlist_id": "wl_x", "level": 1},
        {"condition": "crosses_above", "ticker": "=BAD", "level": 1},
    ],
)
def test_invalid_rules_are_refused(client, people, body):
    r = client.post("/api/price-alerts", json=body, headers=people["alice"]["headers"])
    assert r.status_code == 422


def test_a_watchlist_must_be_yours(client, people):
    alice, bob = people["alice"], people["bob"]
    wid = client.post(
        "/api/watchlists", json={"name": "mine", "tickers": ["UP.US"]}, headers=bob["headers"]
    ).json()["id"]
    r = client.post(
        "/api/price-alerts",
        json={"condition": "moves_pct", "watchlist_id": wid, "pct": 5, "window_days": 5},
        headers=alice["headers"],
    )
    assert r.status_code == 404


def test_the_operator_check_fires_into_the_owners_feed(client, people):
    alice, ada = people["alice"], people["ada"]
    # UP.US rises from 100 to 200 over the seeded window; the last two bars
    # straddle 199.5.
    rid = client.post(
        "/api/price-alerts",
        json={"condition": "crosses_above", "ticker": "UP.US", "level": 199.5},
        headers=alice["headers"],
    ).json()["id"]
    assert client.post("/api/price-alerts/evaluate", headers=alice["headers"]).status_code == 403
    run = client.post("/api/price-alerts/evaluate", json={}, headers=ada["headers"])
    assert run.status_code == 200, run.text
    assert run.json()["fired"] == 1 and run.json()["checked"] == 1
    events = client.get("/api/price-alerts/events", headers=alice["headers"]).json()
    assert events["total"] == 1 and events["items"][0]["rule_id"] == rid
    assert client.get("/api/price-alerts/events", headers=ada["headers"]).json()["total"] == 0
    feed = client.get("/api/notifications", headers=alice["headers"]).json()
    assert any("UP.US crossed above 199.5" in n["title"] for n in feed["items"])
    seen = client.get(f"/api/price-alerts/{rid}", headers=alice["headers"]).json()["last_seen"]
    assert seen["UP.US"]["observed_at"] == "2026-04-01"
