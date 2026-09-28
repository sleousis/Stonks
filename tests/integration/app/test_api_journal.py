"""Round-trip journal routes (roadmap 23.3): trades, the review, the P&L
calendar, the breakdown and playbooks read the caller's own book, another
user's trade or playbook is a 404, and writes need portfolio.manage."""

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


def _fill(state, pf, cid, ticker, side, price, day, *, context=None) -> int:
    ts = f"2026-03-{day:02d}T15:00:00+00:00"
    state.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status, created_at,"
        " updated_at, portfolio_id, origin, decision_context_json)"
        " VALUES (?, ?, ?, 10, 'market', 'filled', ?, ?, ?, 'manual', ?)",
        [cid, ticker, side, ts, ts, pf, json.dumps(context) if context else None],
    )
    cur = state.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at, portfolio_id)"
        " VALUES (?, ?, 10, ?, 0, ?, ?)",
        [cid, ticker, price, ts, pf],
    )
    return int(cur.lastrowid or 0)


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
        trade_a = _fill(
            state, pf_a, "a1", "UP.US", "buy", 100.0, 2, context={"plan": {"stop": 95.0}}
        )
        _fill(state, pf_a, "a2", "UP.US", "sell", 110.0, 5)
        trade_b = _fill(state, pf_b, "b1", "BOBONLY.US", "buy", 50.0, 3)
    tokens = {}
    for name, uid, role in (
        ("alice", alice, Role.TRADER),
        ("bob", bob, Role.TRADER),
        ("vera", viewer, Role.VIEWER),
    ):
        scopes = ["read", "trade"] if role is Role.TRADER else ["read"]
        _, token = auth.create_token(session_principal(uid, role), name="t", scopes=scopes)
        tokens[name] = {"Authorization": f"Bearer {token}"}
    return {"pf_a": pf_a, "pf_b": pf_b, "trade_a": trade_a, "trade_b": trade_b, **tokens}


def test_trades_list_your_own_round_trips(app, people):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        resp = c.get("/api/journal/trades", headers=people["alice"])
        assert resp.status_code == 200, resp.text
        page = resp.json()
        assert page["total"] == 1
        [leg] = page["items"]
        assert leg["trade_id"] == people["trade_a"]
        assert leg["sleeve"] == "manual"
        assert leg["pnl"] == pytest.approx(100.0)
        assert leg["pnl_base"] == pytest.approx(100.0)
        assert leg["r_multiple"] == pytest.approx(2.0)
        assert leg["stop_source"] == "order_plan"
        assert leg["holding_days"] == pytest.approx(3.0)
        assert "BOBONLY" not in resp.text
        other = c.get(
            "/api/journal/trades", params={"portfolio_id": people["pf_b"]}, headers=people["alice"]
        )
        assert other.status_code == 404
        open_only = c.get(
            "/api/journal/trades", params={"status": "open"}, headers=people["alice"]
        ).json()
        assert open_only["total"] == 0


def test_trade_detail_is_404_for_another_users_trade(app, people):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        mine = c.get(f"/api/journal/trades/{people['trade_a']}", headers=people["alice"])
        assert mine.status_code == 200, mine.text
        assert len(mine.json()["legs"]) == 1
        theirs = c.get(f"/api/journal/trades/{people['trade_b']}", headers=people["alice"])
        assert theirs.status_code == 404


def test_review_tags_a_trade_and_splits_results(app, people):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        pb = c.post(
            "/api/journal/playbooks",
            json={"name": "Breakout", "description": "buy the high"},
            headers=people["alice"],
        )
        assert pb.status_code == 201, pb.text
        body = {
            "tags": ["Earnings"],
            "mistakes": ["late entry"],
            "playbook_id": pb.json()["id"],
            "followed_plan": False,
            "review": "chased the gap",
        }
        url = f"/api/journal/trades/{people['trade_a']}/review"
        saved = c.put(url, json=body, headers=people["alice"])
        assert saved.status_code == 200, saved.text
        assert saved.json()["tags"] == ["earnings"]
        leg = c.get("/api/journal/trades", headers=people["alice"]).json()["items"][0]
        assert (leg["playbook_name"], leg["followed_plan"]) == ("Breakout", False)
        tagged = c.get(
            "/api/journal/trades", params={"tag": "earnings"}, headers=people["alice"]
        ).json()
        assert tagged["total"] == 1
        plan = c.get(
            "/api/journal/breakdown", params={"by": "plan"}, headers=people["alice"]
        ).json()
        assert [(g["key"], g["trades"], g["avg_r"]) for g in plan["groups"]] == [
            ("broke", 1, pytest.approx(2.0))
        ]
        by_book = c.get(
            "/api/journal/breakdown", params={"by": "playbook"}, headers=people["alice"]
        ).json()
        assert by_book["groups"][0]["key"] == "Breakout"
        labels = c.get("/api/journal/labels", headers=people["alice"]).json()
        assert labels == {"tags": ["earnings"], "mistakes": ["late entry"]}
        # bob can neither review alice's trade nor use it through his book
        assert c.put(url, json=body, headers=people["bob"]).status_code == 404
        assert (
            c.put(
                url, json=body, params={"portfolio_id": people["pf_a"]}, headers=people["bob"]
            ).status_code
            == 404
        )
        # a viewer may not review
        assert c.put(url, json={}, headers=people["vera"]).status_code == 403
        bad = c.get("/api/journal/breakdown", params={"by": "colour"}, headers=people["alice"])
        assert bad.status_code == 422


def test_another_users_playbook_cannot_be_used_or_edited(app, people):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        bobs = c.post("/api/journal/playbooks", json={"name": "Secret"}, headers=people["bob"])
        pid = bobs.json()["id"]
        url = f"/api/journal/trades/{people['trade_a']}/review"
        used = c.put(url, json={"playbook_id": pid}, headers=people["alice"])
        assert used.status_code == 404
        edit = c.patch(
            f"/api/journal/playbooks/{pid}", json={"archived": True}, headers=people["alice"]
        )
        assert edit.status_code == 404
        assert c.get("/api/journal/playbooks", headers=people["alice"]).json() == []
        archived = c.patch(
            f"/api/journal/playbooks/{pid}", json={"archived": True}, headers=people["bob"]
        )
        assert archived.status_code == 200 and archived.json()["archived"] is True
        dup = c.post("/api/journal/playbooks", json={"name": "Secret"}, headers=people["bob"])
        assert dup.status_code == 422


def test_calendar_books_realised_pnl_on_the_exit_day(app, people):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        cal = c.get("/api/journal/calendar", headers=people["alice"])
        assert cal.status_code == 200, cal.text
        body = cal.json()
        assert body["base_currency"] == "USD"
        assert [(d["key"], d["pnl"]) for d in body["days"]] == [("2026-03-05", 100.0)]
        assert [w["key"] for w in body["weeks"]] == ["2026-W10"]
        assert [m["key"] for m in body["months"]] == ["2026-03"]
        assert body["total"] == pytest.approx(100.0)
