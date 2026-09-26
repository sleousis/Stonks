"""AS-02: the tick ledger is global, but what it says about a portfolio is
not. ``GET /api/ticks``, ``GET /api/ticks/{id}`` and the MCP tick tools
show each caller the global outcome (status, counts, winner, shadow) plus
the slices of their own portfolios only: never another trader's portfolio
id, orders, clipped orders or stale buys."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from stonks.accounts import DEFAULT_OWNER_ID, PortfolioRepository, Role, Scope
from stonks.api import create_app
from stonks.app.context import AppContext
from stonks.app.services import Services
from stonks.auth import AuthService
from stonks.mcp.server import build_server
from stonks.security import KeyRing, SecretBox, generate_key
from stonks.store.state import SqliteState
from tests.integration.app.test_mcp_server import _api, call
from tests.integration.auth.helpers import add_user, make_service, session_principal

SECRET = "SECRET.US"
MULTI = "tick_2026-03-23_multi"
SINGLE = "tick_2026-03-24_single"


def _adjustment(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "side": "buy",
        "rule": "max_weight_per_ticker",
        "original_quantity": 9.0,
        "adjusted_quantity": 7.0,
        "reason": "clipped",
    }


def _order(state, client_id, tick_id, portfolio_id, ticker):
    state.execute(
        "INSERT INTO orders (client_id, tick_id, strategy_id, ticker, side, quantity,"
        " order_type, status, created_at, updated_at, portfolio_id)"
        " VALUES (?, ?, 'bah_active', ?, 'buy', 7, 'market', 'filled', ?, ?, ?)",
        [client_id, tick_id, ticker, "2026-03-23T21:00:00", "2026-03-23T21:00:00", portfolio_id],
    )


@pytest.fixture
def world(settings, seeded):
    """Trader A and trader B each own a book. A multi-book tick traded both;
    a single-book tick traded pf_default (the bootstrap admin's)."""
    a = add_user(settings.state.path, "a@example.com", Role.TRADER)
    b = add_user(settings.state.path, "b@example.com", Role.TRADER)
    with SqliteState(settings.state.path) as state:
        repo = PortfolioRepository(state)
        pf_a = repo.create(Scope(user_id=a, role=Role.TRADER), name="A book").id
        pf_b = repo.create(Scope(user_id=b, role=Role.TRADER), name="B book").id
        multi = {
            "orders_placed": 2,
            "fills": 2,
            "shadow": [],
            "portfolios": {
                pf_a: {"status": "ok", "orders_placed": 1, "risk_adjustments": []},
                pf_b: {
                    "status": "ok",
                    "orders_placed": 1,
                    "risk_adjustments": [_adjustment(SECRET)],
                    "stale_buys_dropped": [SECRET],
                },
            },
        }
        single = {
            "winner_strategy_id": "bah_active",
            "orders_placed": 1,
            "fills": 1,
            "risk_adjustments": [_adjustment(SECRET)],
            "stale_buys_dropped": [SECRET],
            "halted": {"halt": "buys", "gate": "risk_halts", "reason": "kill (user x)"},
            "shadow": [],
        }
        for tick_id, summary in ((MULTI, multi), (SINGLE, single)):
            state.execute(
                "INSERT INTO tick_runs (id, started_at, finished_at, status, summary_json)"
                " VALUES (?, ?, ?, 'ok', ?)",
                [tick_id, "2026-03-24T21:00:00", "2026-03-24T21:01:00", json.dumps(summary)],
            )
        _order(state, "2026-03-23:pf_a:bah_active:UP.US:buy", MULTI, pf_a, "UP.US")
        _order(state, f"2026-03-23:{pf_b}:bah_active:{SECRET}:buy", MULTI, pf_b, SECRET)
        _order(state, f"2026-03-24:bah_active:{SECRET}:buy", SINGLE, "pf_default", SECRET)
    return {"a": a, "b": b, "pf_a": pf_a, "pf_b": pf_b}


@pytest.fixture
def auth(settings, seeded) -> AuthService:
    box = SecretBox(KeyRing.parse(f"k1:{generate_key()}"))
    return make_service(
        settings.state.path, legacy="test-token-123", box=box, clock=lambda: datetime.now(UTC)
    )


@pytest.fixture
def client(settings, seeded, fake_source, auth):
    settings.api.allowed_hosts = ["testserver"]
    svc = Services.create(AppContext(settings, source_factory=lambda: fake_source))
    app = create_app(settings, services=svc)
    app.state.auth = auth
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        yield c


def _token(auth, user_id: str, role: Role) -> str:
    _, token = auth.create_token(session_principal(user_id, role), name="t", scopes=["read"])
    return token


def _get(client, path, token, **params):
    r = client.get(path, headers={"Authorization": f"Bearer {token}"}, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_trader_sees_only_their_own_slice_of_a_multi_book_tick(client, auth, world):
    token = _token(auth, world["a"], Role.TRADER)
    listed = json.dumps(_get(client, "/api/ticks", token))
    detail = _get(client, f"/api/ticks/{MULTI}", token)
    for text in (listed, json.dumps(detail)):
        assert SECRET not in text
        assert world["pf_b"] not in text
    assert set(detail["summary"]["portfolios"]) == {world["pf_a"]}
    assert [o["ticker"] for o in detail["orders"]] == ["UP.US"]
    # the global outcome stays
    assert detail["summary"]["orders_placed"] == 2


def test_the_owner_sees_their_clipped_orders_and_stale_buys(client, auth, world):
    token = _token(auth, world["b"], Role.TRADER)
    detail = _get(client, f"/api/ticks/{MULTI}", token)
    book = detail["summary"]["portfolios"][world["pf_b"]]
    assert book["stale_buys_dropped"] == [SECRET]
    assert [a["ticker"] for a in book["risk_adjustments"]] == [SECRET]
    assert [o["ticker"] for o in detail["orders"]] == [SECRET]


def test_another_users_single_book_tick_shows_the_global_outcome_only(client, auth, world):
    token = _token(auth, world["a"], Role.TRADER)
    detail = _get(client, f"/api/ticks/{SINGLE}", token)
    text = json.dumps(detail)
    assert SECRET not in text and "pf_default" not in text and "kill" not in text
    assert detail["orders"] == []
    assert detail["summary"]["winner_strategy_id"] == "bah_active"
    assert detail["summary"]["orders_placed"] == 1


def test_the_default_books_owner_sees_its_single_book_tick(client, auth, world):
    token = _token(auth, DEFAULT_OWNER_ID, Role.ADMIN)
    detail = _get(client, f"/api/ticks/{SINGLE}", token)
    assert detail["summary"]["stale_buys_dropped"] == [SECRET]
    assert [o["ticker"] for o in detail["orders"]] == [SECRET]


def test_admins_see_other_traders_slices_no_more_than_anyone(client, auth, world):
    token = _token(auth, DEFAULT_OWNER_ID, Role.ADMIN)
    detail = _get(client, f"/api/ticks/{MULTI}", token)
    assert SECRET not in json.dumps(detail) and world["pf_b"] not in json.dumps(detail)


def test_asking_for_another_users_portfolio_is_a_404(client, auth, world):
    token = _token(auth, world["a"], Role.TRADER)
    r = client.get(
        f"/api/ticks/{MULTI}",
        headers={"Authorization": f"Bearer {token}"},
        params={"portfolio_id": world["pf_b"]},
    )
    assert r.status_code == 404
    mine = _get(client, f"/api/ticks/{MULTI}", token, portfolio_id=world["pf_a"])
    assert [o["ticker"] for o in mine["orders"]] == ["UP.US"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_the_mcp_tick_tools_are_scoped_the_same_way(client, auth, world):
    token = _token(auth, world["a"], Role.TRADER)
    async with Client(build_server(_api(client, token=token), max_wait_seconds=60)) as mcp:
        tick = await call(mcp, "get_tick", {"tick_id": MULTI})
        ticks = await call(mcp, "list_ticks")
    for payload in (tick, ticks):
        assert SECRET not in json.dumps(payload)
        assert world["pf_b"] not in json.dumps(payload)
