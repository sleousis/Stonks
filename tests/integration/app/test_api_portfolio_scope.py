"""Portfolio reads are scoped to the caller (step S7): ``portfolio_id`` must
be one of yours (404 otherwise, never 403), the default is your own book,
and admins get aggregate totals only, never another trader's holdings."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from stonks.accounts import PortfolioRepository, Role, Scope
from stonks.api import create_app
from stonks.app.context import AppContext
from stonks.app.services import Services
from stonks.security import KeyRing, SecretBox, generate_key
from stonks.store.state import SqliteState
from tests.integration.app.test_api import AUTH, LOOPBACK, REMOTE
from tests.integration.auth.helpers import add_user, make_service, session_principal

BASE = "https://testserver"
READS = [
    "/api/portfolio",
    "/api/portfolio/snapshots",
    "/api/orders",
    "/api/orders/fills",
    "/api/pnl",
]


@pytest.fixture
def auth(settings, seeded):
    box = SecretBox(KeyRing.parse(f"k1:{generate_key()}"))
    return make_service(
        settings.state.path, legacy="test-token-123", box=box, clock=lambda: datetime.now(UTC)
    )


@pytest.fixture
def app(settings, seeded, fake_source, auth):
    settings.api.allowed_hosts = ["testserver"]
    svc = Services.create(AppContext(settings, source_factory=lambda: fake_source))
    application = create_app(settings, services=svc)
    application.state.auth = auth
    return application


def _book(path, user_id: str, name: str, cash: float, positions: dict[str, float]) -> str:
    with SqliteState(path) as state:
        pf = (
            PortfolioRepository(state)
            .create(Scope(user_id=user_id, role=Role.TRADER), name=name)
            .id
        )
        state.execute(
            "INSERT INTO portfolio_snapshots (taken_at, cash, positions_json, total_value,"
            " portfolio_id) VALUES (?, ?, ?, ?, ?)",
            [
                "2026-03-21T00:00:00+00:00",
                cash,
                json.dumps(positions),
                cash + 1000.0,
                pf,
            ],
        )
    return pf


@pytest.fixture
def people(settings, auth):
    path = settings.state.path
    alice = add_user(path, "alice@example.com")
    bob = add_user(path, "bob@example.com")
    pf_a = _book(path, alice, "Alice", 111.0, {"UP.US": 1})
    pf_b = _book(path, bob, "Bob", 222.0, {"DOWN.US": 7})
    tokens = {}
    for name, uid in (("alice", alice), ("bob", bob)):
        _, token = auth.create_token(session_principal(uid, Role.TRADER), name="t", scopes=["read"])
        tokens[name] = {"Authorization": f"Bearer {token}"}
    return {"pf_a": pf_a, "pf_b": pf_b, **tokens}


def test_another_users_portfolio_is_404_on_every_read(app, people):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        for url in READS:
            resp = c.get(url, params={"portfolio_id": people["pf_b"]}, headers=people["alice"])
            assert resp.status_code == 404, (url, resp.status_code)
            assert "DOWN.US" not in resp.text
            mine = c.get(url, params={"portfolio_id": people["pf_a"]}, headers=people["alice"])
            assert mine.status_code == 200, (url, mine.text)


def test_the_default_is_your_own_book(app, people):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        body = c.get("/api/portfolio", headers=people["alice"]).json()
        assert body["cash"] == 111.0
        assert [p["ticker"] for p in body["positions"]] == ["UP.US"]
        snaps = c.get("/api/portfolio/snapshots", headers=people["bob"]).json()
        assert [s["cash"] for s in snaps["items"]] == [222.0]


def test_a_user_without_a_portfolio_gets_404(app, settings, auth):
    uid = add_user(settings.state.path, "carol@example.com")
    _, token = auth.create_token(session_principal(uid, Role.TRADER), name="t", scopes=["read"])
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        resp = c.get("/api/portfolio", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404


def test_admins_cannot_read_a_traders_holdings(app, people):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        for url in READS:
            resp = c.get(url, params={"portfolio_id": people["pf_a"]}, headers=AUTH)
            assert resp.status_code == 404, (url, resp.status_code)
        # The admin's own book (pf_default) still reads as before.
        assert c.get("/api/portfolio", headers=AUTH).status_code == 200


def test_admins_see_aggregate_totals_only(app, people, settings):
    carol = add_user(settings.state.path, "carol@example.com")
    _book(settings.state.path, carol, "Carol", 333.0, {"FLAT.US": 2})
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        totals = c.get("/api/portfolio/totals", headers=AUTH)
        assert totals.status_code == 200, totals.text
        body = totals.json()
        assert body["portfolios"] >= 3 and body["owners"] >= 3
        assert body["suppressed"] is False
        assert body["cash"] >= 111.0 + 222.0 + 333.0
        assert "UP.US" not in totals.text and "DOWN.US" not in totals.text
        assert c.get("/api/portfolio/totals", headers=people["alice"]).status_code == 403


def test_portfolio_reads_need_a_credential_even_on_loopback(app):
    with TestClient(app, client=LOOPBACK, base_url=BASE) as c:
        for url in READS:
            assert c.get(url).status_code == 401, url


def test_totals_hide_money_below_three_other_owners(app, people):
    """BE-43: with two other traders the admin could subtract their way to
    one person's numbers, so the money figures are withheld."""
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        for url in ("/api/portfolio/totals", "/api/insights/totals"):
            body = c.get(url, headers=AUTH).json()
            assert body["suppressed"] is True, url
            assert body["cash"] == 0.0 and body["total_value"] == 0.0, url


def test_a_paper_account_does_not_count_as_another_portfolio(app, people, settings):
    """BE-43: a broker book's paper copy is the same person's book."""
    path = settings.state.path
    with SqliteState(path) as state:
        owner = state.sql("SELECT owner_id FROM portfolios WHERE id = ?", [people["pf_a"]])
    paper = _book(path, owner[0]["owner_id"], "Alice paper", 50.0, {})
    with SqliteState(path) as state:
        state.execute("UPDATE portfolios SET paper_of = ? WHERE id = ?", [people["pf_a"], paper])
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        before = c.get("/api/portfolio/totals", headers=AUTH).json()
        insights = c.get("/api/insights/totals", headers=AUTH).json()
    names = {r["id"] for r in _active(path)} - {paper}
    assert before["portfolios"] == len(names)
    assert insights["portfolios"] == len(names)


def _active(path):
    with SqliteState(path) as state:
        return state.sql(
            "SELECT p.id FROM portfolios p WHERE p.status = 'active' AND EXISTS ("
            "SELECT 1 FROM portfolio_snapshots s WHERE s.portfolio_id = p.id)"
        )
