"""TCA and journal routes (BL-32): summaries and the journal read the
caller's own book, another user's order or note is a 404, and notes need a
credential that may manage a portfolio."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from stonks.accounts import PortfolioRepository, Role, Scope
from stonks.api import create_app
from stonks.app.context import AppContext
from stonks.app.services import Services
from stonks.security import KeyRing, SecretBox, generate_key
from stonks.store.state import SqliteState
from tests.integration.app.test_api import REMOTE
from tests.integration.auth.helpers import add_user, make_service, session_principal

BASE = "https://testserver"


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


def _order(state, pf: str, client_id: str, ticker: str, fill_price: float) -> None:
    state.execute(
        "INSERT INTO orders (client_id, strategy_id, ticker, side, quantity, order_type,"
        " status, created_at, updated_at, portfolio_id, decision_price, decided_at,"
        " decision_context_json, expected_cost_bps)"
        " VALUES (?, NULL, ?, 'buy', 10, 'market', 'filled', ?, ?, ?, 100.0, ?, ?, 5.0)",
        [
            client_id,
            ticker,
            "2026-03-20T00:00:00+00:00",
            "2026-03-20T00:00:00+00:00",
            pf,
            "2026-03-20T00:00:00+00:00",
            '{"trigger": "signal", "strategy_id": "mom", "score": 0.04, "rank": 1}',
        ],
    )
    state.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at,"
        " portfolio_id, arrival_price) VALUES (?, ?, 10, ?, 0, ?, ?, 100.0)",
        [client_id, ticker, fill_price, "2026-03-20T00:00:00+00:00", pf],
    )


@pytest.fixture
def people(settings, auth):
    path = settings.state.path
    alice = add_user(path, "alice@example.com")
    bob = add_user(path, "bob@example.com")
    viewer = add_user(path, "vera@example.com", role=Role.VIEWER)
    with SqliteState(path) as state:
        repo = PortfolioRepository(state)
        pf_a = repo.create(Scope(user_id=alice, role=Role.TRADER), name="Alice").id
        pf_b = repo.create(Scope(user_id=bob, role=Role.TRADER), name="Bob").id
        _order(state, pf_a, "a1", "UP.US", 100.5)
        _order(state, pf_b, "b1", "DOWN.US", 101.0)
    tokens = {}
    for name, uid, role in (
        ("alice", alice, Role.TRADER),
        ("bob", bob, Role.TRADER),
        ("vera", viewer, Role.VIEWER),
    ):
        scopes = ["read", "trade"] if role is Role.TRADER else ["read"]
        _, token = auth.create_token(session_principal(uid, role), name="t", scopes=scopes)
        tokens[name] = {"Authorization": f"Bearer {token}"}
    return {"pf_a": pf_a, "pf_b": pf_b, **tokens}


def test_summary_reads_your_own_book(app, people):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        resp = c.get("/api/tca/summary", params={"by": "ticker"}, headers=people["alice"])
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["portfolio_id"] == people["pf_a"]
        [group] = body["groups"]
        assert group["key"] == "UP.US"
        assert group["is_bps"] == pytest.approx(50.0)
        assert group["expected_bps"] == pytest.approx(5.0)
        assert group["model_gap_bps"] == pytest.approx(45.0)
        other = c.get(
            "/api/tca/summary", params={"portfolio_id": people["pf_b"]}, headers=people["alice"]
        )
        assert other.status_code == 404
        bad = c.get("/api/tca/summary", params={"by": "colour"}, headers=people["alice"])
        assert bad.status_code == 422


def test_journal_lists_reason_context_and_outcome(app, people):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        page = c.get("/api/tca/journal", headers=people["alice"]).json()
    assert page["total"] == 1
    [entry] = page["items"]
    assert entry["client_id"] == "a1"
    assert entry["trigger"] == "signal"
    assert entry["context"]["score"] == 0.04
    assert entry["shortfall"]["impact_bps"] == pytest.approx(50.0)
    assert "DOWN.US" not in str(page)


def test_order_detail_is_404_for_another_users_order(app, people):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        mine = c.get("/api/tca/orders/a1", headers=people["alice"])
        assert mine.status_code == 200, mine.text
        assert mine.json()["shortfall"]["is_bps"] == pytest.approx(50.0)
        assert c.get("/api/tca/orders/b1", headers=people["alice"]).status_code == 404
        assert c.get("/api/tca/orders/nope", headers=people["alice"]).status_code == 404


def test_notes_are_added_edited_by_their_author_only(app, people):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        created = c.post(
            "/api/tca/orders/a1/notes", json={"note": "entered late"}, headers=people["alice"]
        )
        assert created.status_code == 201, created.text
        note = created.json()
        assert note["author"].startswith("user:")
        edited = c.put(
            f"/api/tca/notes/{note['id']}",
            json={"note": "entered late, gap up"},
            headers=people["alice"],
        )
        assert edited.status_code == 200
        assert edited.json()["note"] == "entered late, gap up"
        detail = c.get("/api/tca/orders/a1", headers=people["alice"]).json()
        assert [n["note"] for n in detail["notes"]] == ["entered late, gap up"]
        # bob can neither see nor touch alice's order or note
        assert (
            c.post("/api/tca/orders/a1/notes", json={"note": "x"}, headers=people["bob"])
        ).status_code == 404
        assert (
            c.put(f"/api/tca/notes/{note['id']}", json={"note": "x"}, headers=people["bob"])
        ).status_code == 404
        assert (
            c.post("/api/tca/orders/a1/notes", json={"note": " "}, headers=people["alice"])
        ).status_code in (400, 422)


def test_a_viewer_cannot_write_notes(app, people):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        resp = c.post("/api/tca/orders/a1/notes", json={"note": "x"}, headers=people["vera"])
    assert resp.status_code in (401, 403)
