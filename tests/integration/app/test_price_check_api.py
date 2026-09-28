"""The price check over the REST API (roadmap 23.6): the newest check for
Health, and the run the scheduler's api backend starts."""

from __future__ import annotations

from stonks.store.state import SqliteState
from tests.integration.app.test_api import AUTH


def test_no_check_yet_and_a_run_while_off(client):
    got = client.get("/api/health/price-check", headers=AUTH)
    assert got.status_code == 200 and got.json() is None
    ran = client.post("/api/health/price-check/run", json={}, headers=AUTH)
    assert ran.status_code == 200, ran.text
    assert ran.json() == {"ran": False, "reason": "disabled", "check": None}


def test_a_stored_check_is_read_back(client, settings):
    with SqliteState(settings.state.path) as state:
        state.execute(
            "INSERT INTO price_checks (as_of, checked_at, source, status, tickers_checked,"
            " tickers_compared, held_json, items_json, detail) VALUES ('2026-03-20',"
            " '2026-03-20T20:42:00+00:00', 'yahoo', 'gaps', 1, 1, '[\"UP.US\"]', ?, 'x')",
            ['[{"ticker": "UP.US", "status": "gap", "detail": "close 5.0% off"}]'],
        )
    view = client.get("/api/health/price-check", headers=AUTH).json()
    assert view["status"] == "gaps" and view["held"] == ["UP.US"]
    assert view["items"][0]["detail"] == "close 5.0% off"
