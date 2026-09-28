"""The stored universe the production tick trades (``[production].universe``
set to a universe id) decides what every book buys. Changing it is an admin
setting (``settings.manage``, step-up), so a trader's ``lab.run`` must not
change it through the universe routes either."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from stonks.api import create_app
from stonks.app.context import AppContext
from stonks.app.services import Services
from tests.integration.app.multiuser.conftest import BASE
from tests.integration.app.test_api import REMOTE

TRADING = "trade_u"


@pytest.fixture
def client(settings, seeded, fake_source, auth):
    settings.api.allowed_hosts = ["testserver"]
    settings.production.universe = TRADING
    svc = Services.create(AppContext(settings, source_factory=lambda: fake_source))
    application = create_app(settings, services=svc)
    application.state.auth = auth
    with TestClient(application, client=REMOTE, base_url=BASE) as c:
        yield c


def _create(client, people, universe_id: str, spec: dict) -> None:
    body = {"id": universe_id, "kind": "list", "spec": spec}
    resp = client.post("/api/universes", json=body, headers=people["ada"]["headers"])
    assert resp.status_code == 201, resp.text


def test_a_trader_cannot_replace_the_trading_universe(client, people):
    _create(client, people, TRADING, {"tickers": ["UP.US"]})
    body = {"kind": "list", "spec": {"tickers": ["DOWN.US"]}}
    resp = client.put(f"/api/universes/{TRADING}", json=body, headers=people["alice"]["headers"])
    assert resp.status_code == 403, resp.text
    kept = client.get(f"/api/universes/{TRADING}", headers=people["alice"]["headers"]).json()
    assert kept["spec"]["tickers"] == ["UP.US"]


def test_a_trader_still_edits_other_universes(client, people):
    _create(client, people, "research", {"tickers": ["UP.US"]})
    body = {"kind": "list", "spec": {"tickers": ["DOWN.US"]}}
    resp = client.put("/api/universes/research", json=body, headers=people["alice"]["headers"])
    assert resp.status_code == 200, resp.text


def test_an_admin_replaces_the_trading_universe(client, people):
    _create(client, people, TRADING, {"tickers": ["UP.US"]})
    body = {"kind": "list", "spec": {"tickers": ["DOWN.US"]}}
    resp = client.put(f"/api/universes/{TRADING}", json=body, headers=people["ada"]["headers"])
    assert resp.status_code == 200, resp.text


def test_a_trader_cannot_rewrite_the_index_the_trading_universe_follows(client, people):
    body = {"id": TRADING, "kind": "index", "spec": {"index_id": "toy"}}
    resp = client.post("/api/universes", json=body, headers=people["ada"]["headers"])
    assert resp.status_code == 201, resp.text
    content = "date,ticker,action\n2026-01-02,DOWN.US,member\n"
    imported = client.post(
        "/api/universes/index-history",
        json={"index_id": "toy", "format": "csv", "content": content},
        headers=people["alice"]["headers"],
    )
    assert imported.status_code == 403, imported.text
    other = client.post(
        "/api/universes/index-history",
        json={"index_id": "other", "format": "csv", "content": content},
        headers=people["alice"]["headers"],
    )
    assert other.status_code == 200, other.text
