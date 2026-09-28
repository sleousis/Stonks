"""Screen alerts through the REST API (roadmap 23.17): alerts on your own
saved screens only, roles, validation, the operator run and the feed it
writes."""

from __future__ import annotations

import pytest

from stonks.app.screen_alerts import ScreenAlertService
from stonks.notify.router import NotificationRouter
from stonks.store.lake import DuckDBLake
from tests.fixtures.screener import seed_market

SPEC = {"filters": [{"metric": "price", "min": 19.5}]}


@pytest.fixture(autouse=True)
def _service(app, settings, seeded):
    services = app.state.services
    services.screen_alerts = ScreenAlertService(
        services.context, publisher=lambda state: NotificationRouter(state, {}).publish
    )
    with DuckDBLake(settings.lake.path) as lake:
        seed_market(lake)


def _screen(client, who, name="Pricey") -> str:
    made = client.post(
        "/api/screener/screens", json={"name": name, "spec": SPEC}, headers=who["headers"]
    )
    assert made.status_code == 201, made.text
    return made.json()["id"]


def test_turn_an_alert_on_change_it_and_off(client, people):
    alice = people["alice"]
    sid = _screen(client, alice)
    assert (
        client.get(f"/api/screener/screens/{sid}/alert", headers=alice["headers"]).status_code
        == 404
    )
    on = client.put(f"/api/screener/screens/{sid}/alert", json={}, headers=alice["headers"])
    assert on.status_code == 200, on.text
    assert on.json()["cadence"] == "daily" and on.json()["enabled"] and on.json()["matched"] == 0
    weekly = client.put(
        f"/api/screener/screens/{sid}/alert",
        json={"cadence": "weekly", "weekday": 4},
        headers=alice["headers"],
    )
    assert weekly.json()["weekday"] == 4
    listed = client.get("/api/screener/alerts", headers=alice["headers"]).json()
    assert [a["screen_name"] for a in listed["items"]] == ["Pricey"]
    gone = client.delete(f"/api/screener/screens/{sid}/alert", headers=alice["headers"])
    assert gone.status_code == 204
    assert client.get("/api/screener/alerts", headers=alice["headers"]).json()["total"] == 0
    # The screen itself stays.
    assert client.get(f"/api/screener/screens/{sid}", headers=alice["headers"]).status_code == 200


@pytest.mark.parametrize(
    "body",
    [{"cadence": "weekly"}, {"cadence": "daily", "weekday": 1}, {"weekday": 9}, {"x": 1}],
)
def test_bad_alerts_are_refused(client, people, body):
    alice = people["alice"]
    sid = _screen(client, alice)
    r = client.put(f"/api/screener/screens/{sid}/alert", json=body, headers=alice["headers"])
    assert r.status_code == 422


def test_alerts_are_private_and_viewers_only_read(client, people):
    alice, bob, vic, ada = (people[n] for n in ("alice", "bob", "vic", "ada"))
    sid = _screen(client, alice)
    client.put(f"/api/screener/screens/{sid}/alert", json={}, headers=alice["headers"])
    for who in (bob, ada):
        path = f"/api/screener/screens/{sid}/alert"
        assert client.get(path, headers=who["headers"]).status_code == 404
        assert client.put(path, json={}, headers=who["headers"]).status_code == 404
        assert client.delete(path, headers=who["headers"]).status_code == 404
        assert client.get("/api/screener/alerts", headers=who["headers"]).json()["total"] == 0
    assert client.get("/api/screener/alerts", headers=vic["headers"]).status_code == 200
    vic_path = "/api/screener/screens/scr_nope/alert"
    assert client.put(vic_path, json={}, headers=vic["headers"]).status_code == 403


def test_the_operator_run_sets_a_baseline_then_alerts_the_owner(client, people):
    alice, ada = people["alice"], people["ada"]
    sid = _screen(client, alice)
    client.put(f"/api/screener/screens/{sid}/alert", json={}, headers=alice["headers"])
    assert (
        client.post("/api/screener/alerts/evaluate", json={}, headers=alice["headers"]).status_code
        == 403
    )
    first = client.post(
        "/api/screener/alerts/evaluate", json={"as_of": "2024-02-01"}, headers=ada["headers"]
    )
    assert first.status_code == 200, first.text
    assert first.json()["baselines"] == 1 and first.json()["fired"] == 0
    second = client.post(
        "/api/screener/alerts/evaluate", json={"as_of": "2024-12-31"}, headers=ada["headers"]
    ).json()
    assert second["fired"] == 1 and second["published"] == 1
    events = client.get("/api/screener/alerts/events", headers=alice["headers"]).json()
    assert events["total"] == 1
    assert events["items"][0]["tickers"] == ["AAA.US"]
    assert events["items"][0]["screen_name"] == "Pricey"
    assert client.get("/api/screener/alerts/events", headers=ada["headers"]).json()["total"] == 0
    feed = client.get("/api/notifications", headers=alice["headers"]).json()
    assert any("1 new name in Pricey" in n["title"] for n in feed["items"])
    alert = client.get(f"/api/screener/screens/{sid}/alert", headers=alice["headers"]).json()
    assert alert["last_as_of"] == "2024-12-31" and alert["matched"] == 2
