"""Manual orders through the REST API (roadmap 20.1): ownership, roles,
idempotency, halts, refusals with the risk rules' adjustments, and the
second factor for real money."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from stonks.accounts import PortfolioRepository, Role, Scope
from stonks.app.manual_orders import ManualOrdersService
from stonks.store.state import SqliteState
from tests.integration.app.stepup import allow_step_up
from tests.unit.test_manual_orders import WorkingBroker

NOW = datetime(2026, 4, 2, 15, 0, tzinfo=UTC)


@pytest.fixture
def brokers():
    return {}


@pytest.fixture(autouse=True)
def _clock(app, brokers):
    services = app.state.services
    services.manual_orders = ManualOrdersService(
        services.context, clock=lambda: NOW, brokers=lambda p: brokers[p.id]
    )


def _book(settings, user_id: str, name: str = "Book", kind: str = "simulated") -> str:
    with SqliteState(settings.state.path) as state:
        return (
            PortfolioRepository(state)
            .create(
                Scope(user_id=user_id, role=Role.TRADER),
                name=name,
                kind=kind,  # type: ignore[arg-type]
                initial_cash=10_000.0,
            )
            .id
        )


def _body(pid: str, **kw):
    return {
        "portfolio_id": pid,
        "ticker": "UP.US",
        "side": "buy",
        "quantity": 5,
        "reason": "my own idea",
    } | kw


def test_trader_places_a_manual_order_that_fills(client, people, settings):
    alice = people["alice"]
    pid = _book(settings, alice["id"])
    r = client.post("/api/orders/manual", json=_body(pid, client_id="a1"), headers=alice["headers"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "filled" and out["live"] is False
    assert out["client_id"] == f"manual:{pid}:a1"
    again = client.post(
        "/api/orders/manual", json=_body(pid, client_id="a1"), headers=alice["headers"]
    )
    assert again.json()["duplicate"] is True
    orders = client.get(
        "/api/orders", params={"portfolio_id": pid, "origin": "manual"}, headers=alice["headers"]
    ).json()
    assert orders["total"] == 1
    item = orders["items"][0]
    assert item["origin"] == "manual" and item["manual_reason"] == "my own idea"
    assert item["placed_by"] == f"user:{alice['id']}" and item["strategy_id"] is None


def test_preview_places_nothing(client, people, settings):
    alice = people["alice"]
    pid = _book(settings, alice["id"])
    r = client.post("/api/orders/manual/preview", json=_body(pid), headers=alice["headers"])
    assert r.status_code == 200 and r.json()["status"] == "preview"
    assert (
        client.get("/api/orders", params={"portfolio_id": pid}, headers=alice["headers"]).json()[
            "total"
        ]
        == 0
    )


def test_viewer_is_refused(client, people, settings):
    vic = people["vic"]
    r = client.post("/api/orders/manual", json=_body("pf_default"), headers=vic["headers"])
    assert r.status_code == 403


def test_someone_elses_portfolio_is_not_found(client, people, settings):
    pid = _book(settings, people["alice"]["id"])
    for who in ("bob", "ada"):
        r = client.post("/api/orders/manual", json=_body(pid), headers=people[who]["headers"])
        assert r.status_code == 404, who


def test_kill_switch_refuses(client, people, settings):
    alice = people["alice"]
    pid = _book(settings, alice["id"])
    kill = client.post(
        "/api/halts/kill",
        json={"scope": "portfolio", "portfolio_id": pid, "reason": "stop"},
        headers=alice["headers"],
    )
    assert kill.status_code in (200, 201), kill.text
    r = client.post("/api/orders/manual", json=_body(pid), headers=alice["headers"])
    assert r.status_code == 409 and r.json()["code"] == "order_refused"
    assert "halted" in r.json()["detail"]


def test_risk_refusal_lists_the_adjustments(client, people, settings):
    alice = people["alice"]
    with SqliteState(settings.state.path) as state:
        pid = (
            PortfolioRepository(state)
            .create(
                Scope(user_id=alice["id"], role=Role.TRADER),
                name="Capped",
                initial_cash=10_000.0,
                risk_policy={"max_weight_per_ticker": 0.01},
            )
            .id
        )
    r = client.post("/api/orders/manual", json=_body(pid), headers=alice["headers"])
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "order_refused" and body["risk_adjustments"][0]["rule"]
    ok = client.post(
        "/api/orders/manual", json=_body(pid, allow_reduce=True), headers=alice["headers"]
    )
    assert ok.status_code == 200 and ok.json()["quantity"] < 5


def test_limit_order_needs_a_price(client, people, settings):
    alice = people["alice"]
    pid = _book(settings, alice["id"])
    r = client.post(
        "/api/orders/manual", json=_body(pid, order_type="limit"), headers=alice["headers"]
    )
    assert r.status_code == 422


def test_real_money_needs_a_fresh_second_factor(app, client, people, settings, brokers):
    alice = people["alice"]
    pid = _book(settings, alice["id"], name="Live", kind="broker")
    brokers[pid] = WorkingBroker()
    body = _body(pid, order_type="limit", limit_price=95.0)
    r = client.post("/api/orders/manual", json=body, headers=alice["headers"])
    assert r.status_code == 403 and r.json()["code"] == "step_up_required"
    # a preview cannot trade, so it needs no second factor
    preview = client.post("/api/orders/manual/preview", json=body, headers=alice["headers"])
    assert preview.status_code == 200 and preview.json()["live"] is True
    allow_step_up(app)
    placed = client.post("/api/orders/manual", json=body, headers=alice["headers"])
    assert placed.status_code == 200, placed.text
    out = placed.json()
    assert out["status"] == "pending" and out["live"] is True
    changed = client.post(
        f"/api/orders/{out['client_id']}/change",
        json={"portfolio_id": pid, "quantity": 3, "reason": "smaller"},
        headers=alice["headers"],
    )
    assert changed.status_code == 200, changed.text
    new_id = changed.json()["client_id"]
    assert new_id == f"{out['client_id']}.r1"
    cancelled = client.post(
        f"/api/orders/{new_id}/cancel",
        json={"portfolio_id": pid, "reason": "done"},
        headers=alice["headers"],
    )
    assert cancelled.status_code == 200 and cancelled.json()["cancelled"] is True


class ClosingBroker(WorkingBroker):
    """Counts closes: an IBKR broker holds the API's client id until closed."""

    def __init__(self) -> None:
        super().__init__()
        self.closes = 0

    def close(self) -> None:
        self.closes += 1


def test_each_request_closes_the_broker_it_opened(app, client, people, settings, brokers):
    """Roadmap 19.17: the next request can open the API's client id again."""
    alice = people["alice"]
    pid = _book(settings, alice["id"], name="Live", kind="broker")
    broker = brokers[pid] = ClosingBroker()
    allow_step_up(app)
    body = _body(pid, order_type="limit", limit_price=95.0)
    client.post("/api/orders/manual/preview", json=body, headers=alice["headers"])
    assert broker.closes == 1
    out = client.post("/api/orders/manual", json=body, headers=alice["headers"]).json()
    assert broker.closes == 2
    changed = client.post(
        f"/api/orders/{out['client_id']}/change",
        json={"portfolio_id": pid, "quantity": 3, "reason": "smaller"},
        headers=alice["headers"],
    ).json()
    assert broker.closes == 3
    client.post(
        f"/api/orders/{changed['client_id']}/cancel",
        json={"portfolio_id": pid, "reason": "done"},
        headers=alice["headers"],
    )
    assert broker.closes == 4


def test_cancel_of_a_filled_order_is_a_conflict(client, people, settings):
    alice = people["alice"]
    pid = _book(settings, alice["id"])
    placed = client.post("/api/orders/manual", json=_body(pid), headers=alice["headers"]).json()
    r = client.post(
        f"/api/orders/{placed['client_id']}/cancel",
        json={"portfolio_id": pid, "reason": "late"},
        headers=alice["headers"],
    )
    assert r.status_code == 409


def test_bob_cannot_cancel_alices_order(client, people, settings):
    alice, bob = people["alice"], people["bob"]
    pid = _book(settings, alice["id"])
    placed = client.post("/api/orders/manual", json=_body(pid), headers=alice["headers"]).json()
    r = client.post(
        f"/api/orders/{placed['client_id']}/cancel",
        json={"portfolio_id": pid, "reason": "mine now"},
        headers=bob["headers"],
    )
    assert r.status_code == 404


def test_plan_sizes_an_entry_and_the_order_keeps_its_stop(client, people, settings):
    alice = people["alice"]
    pid = _book(settings, alice["id"])
    plan = client.post(
        "/api/orders/manual/plan",
        json={
            "portfolio_id": pid,
            "ticker": "UP.US",
            "side": "buy",
            "stop_price": 180.0,
            "target_price": 260.0,
            "risk_percent": 1.0,
        },
        headers=alice["headers"],
    )
    assert plan.status_code == 200, plan.text
    got = plan.json()
    assert got["quantity"] == int(got["risk_budget"] // got["risk_per_share"])
    assert got["entry_is_close"] is True and got["reward_risk"] > 0
    bad = client.post(
        "/api/orders/manual/plan",
        json={"portfolio_id": pid, "ticker": "UP.US", "side": "buy", "stop_price": 180.0},
        headers=alice["headers"],
    )
    assert bad.status_code == 422
    r = client.post(
        "/api/orders/manual",
        json=_body(pid, quantity=got["quantity"], stop_price=180.0, target_price=260.0),
        headers=alice["headers"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["stop_price"] == 180.0 and r.json()["reward_risk"] > 0
    wrong = client.post(
        "/api/orders/manual/preview", json=_body(pid, stop_price=1e6), headers=alice["headers"]
    )
    assert wrong.status_code == 409 and "below the entry" in wrong.json()["detail"]
    viewer = client.post(
        "/api/orders/manual/plan",
        json={"ticker": "UP.US", "side": "buy", "stop_price": 1.0, "risk_amount": 5.0},
        headers=people["vic"]["headers"],
    )
    assert viewer.status_code == 403
