"""S7: your portfolios, whether each trades paper or live, and your
subscriptions (list, subscribe, enable, disable, change mode with the auto
gate)."""

from __future__ import annotations

import pytest

from stonks.accounts import DEFAULT_PORTFOLIO_ID, PortfolioRepository, Scope
from stonks.store.state import SqliteState
from tests.integration.app.stepup import allow_step_up
from tests.integration.app.test_api import AUTH


def _portfolio(settings, person: dict, name: str, kind: str = "simulated") -> str:
    with SqliteState(settings.state.path) as state:
        scope = Scope(user_id=person["id"], role=person["role"])
        return PortfolioRepository(state).create(scope, name=name, kind=kind).id


# ---- portfolios ------------------------------------------------------------------


def test_portfolios_lists_only_your_own(client, settings, people):
    alice_pf = _portfolio(settings, people["alice"], "Alice book")
    _portfolio(settings, people["bob"], "Bob book")
    alice = client.get("/api/portfolios", headers=people["alice"]["headers"])
    assert alice.status_code == 200
    assert [p["id"] for p in alice.json()["items"]] == [alice_pf]
    assert alice.json()["items"][0]["trading"] == "paper"
    assert "Bob book" not in alice.text
    # Admins see their own books too, never other people's.
    ada = client.get("/api/portfolios", headers=people["ada"]["headers"]).json()
    assert ada["items"] == [] and ada["total"] == 0
    owner = client.get("/api/portfolios", headers=AUTH).json()["items"]
    assert [p["id"] for p in owner] == [DEFAULT_PORTFOLIO_ID]


def test_portfolios_mark_the_one_reads_use_by_default(client, settings, people):
    _portfolio(settings, people["alice"], "First")
    _portfolio(settings, people["alice"], "Second")
    listed = client.get("/api/portfolios", headers=people["alice"]["headers"]).json()["items"]
    assert sum(p["is_default"] for p in listed) == 1
    owner = client.get("/api/portfolios", headers=AUTH).json()["items"]
    assert owner[0]["is_default"] is True


def test_trading_modes_say_paper_or_live_per_portfolio(client, settings, people):
    sim = _portfolio(settings, people["alice"], "Sim")
    mirror = _portfolio(settings, people["alice"], "Mirror", kind="broker")
    modes = client.get("/api/portfolios/trading-modes", headers=people["alice"]["headers"])
    assert modes.status_code == 200, modes.text
    by_id = {m["portfolio_id"]: m for m in modes.json()["items"]}
    assert set(by_id) == {sim, mirror}
    assert (by_id[sim]["trading"], by_id[sim]["broker"]) == ("paper", "simulated")
    assert (by_id[mirror]["trading"], by_id[mirror]["broker"]) == ("live", "connection")
    default = client.get("/api/portfolios/trading-modes", headers=AUTH).json()["items"]
    assert [(m["portfolio_id"], m["trading"], m["broker"]) for m in default] == [
        (DEFAULT_PORTFOLIO_ID, "paper", "simulated")
    ]


@pytest.mark.parametrize(
    ("paper", "allow_live", "trading"), [(True, False, "paper"), (False, True, "live")]
)
def test_the_default_book_follows_the_alpaca_endpoint(client, settings, paper, allow_live, trading):
    settings.brokers.kind = "alpaca"
    settings.brokers.alpaca.paper = paper
    settings.brokers.alpaca.allow_live = allow_live
    [mode] = client.get("/api/portfolios/trading-modes", headers=AUTH).json()["items"]
    assert (mode["trading"], mode["broker"]) == (trading, "alpaca")


# ---- subscriptions ---------------------------------------------------------------


def test_subscribe_list_and_scope(client, settings, people):
    alice, bob = people["alice"], people["bob"]
    pf = _portfolio(settings, alice, "Book")
    created = client.post(
        "/api/subscriptions",
        json={"strategy_id": "bah_active", "portfolio_id": pf, "mode": "paper"},
        headers=alice["headers"],
    )
    assert created.status_code == 201, created.text
    sub = created.json()
    assert sub["mode"] == "paper" and sub["enabled"] is True
    assert sub["strategy_status"] == "active"
    assert sub["paper_days_completed"] == 0 and sub["paper_days_required"] == 20
    assert any("paper trading days" in b for b in sub["auto_blockers"])
    assert sub["paused_reason"] is None

    listed = client.get("/api/subscriptions", headers=alice["headers"])
    assert [s["id"] for s in listed.json()["items"]] == [sub["id"]]
    assert client.get("/api/subscriptions", headers=bob["headers"]).json()["items"] == []

    # Bob can't subscribe Alice's portfolio, nor see or change her subscription.
    stolen = client.post(
        "/api/subscriptions",
        json={"strategy_id": "bah_active", "portfolio_id": pf, "mode": "paper"},
        headers=bob["headers"],
    )
    assert stolen.status_code == 404
    patched = client.patch(
        f"/api/subscriptions/{sub['id']}", json={"enabled": False}, headers=bob["headers"]
    )
    assert patched.status_code == 404


def test_subscribe_refusals(client, settings, people):
    alice = people["alice"]
    pf = _portfolio(settings, alice, "Book")
    viewer = client.post(
        "/api/subscriptions",
        json={"strategy_id": "bah_active", "mode": "notify"},
        headers=people["vic"]["headers"],
    )
    assert viewer.status_code == 403
    unknown = client.post(
        "/api/subscriptions",
        json={"strategy_id": "nope", "mode": "notify"},
        headers=alice["headers"],
    )
    assert unknown.status_code == 404
    no_book = client.post(
        "/api/subscriptions",
        json={"strategy_id": "bah_active", "mode": "paper"},
        headers=alice["headers"],
    )
    assert no_book.status_code == 422
    auto = client.post(
        "/api/subscriptions",
        json={"strategy_id": "bah_active", "portfolio_id": pf, "mode": "auto"},
        headers=alice["headers"],
    )
    assert auto.status_code == 409 and auto.json()["code"] == "auto_blocked"
    assert auto.json()["blockers"]


def test_disable_enable_and_notify(client, settings, people):
    alice = people["alice"]
    pf = _portfolio(settings, alice, "Book")
    sub = client.post(
        "/api/subscriptions",
        json={"strategy_id": "bah_active", "portfolio_id": pf, "mode": "paper"},
        headers=alice["headers"],
    ).json()
    url = f"/api/subscriptions/{sub['id']}"
    off = client.patch(url, json={"enabled": False, "reason": "away"}, headers=alice["headers"])
    assert off.status_code == 200 and off.json()["enabled"] is False
    on = client.patch(url, json={"enabled": True}, headers=alice["headers"])
    assert on.json()["enabled"] is True
    notify = client.patch(url, json={"mode": "notify"}, headers=alice["headers"])
    assert notify.json()["mode"] == "notify"


def test_auto_needs_a_step_up_then_the_paper_record(app, client, settings, people):
    alice = people["alice"]
    pf = _portfolio(settings, alice, "Live", kind="broker")
    sub = client.post(
        "/api/subscriptions",
        json={"strategy_id": "bah_active", "portfolio_id": pf, "mode": "paper"},
        headers=alice["headers"],
    ).json()
    url = f"/api/subscriptions/{sub['id']}"
    token = client.patch(url, json={"mode": "auto"}, headers=alice["headers"])
    assert token.status_code == 403 and token.json()["code"] == "step_up_required"

    allow_step_up(app)
    blocked = client.patch(url, json={"mode": "auto"}, headers=alice["headers"])
    assert blocked.status_code == 409, blocked.text
    body = blocked.json()
    assert body["code"] == "auto_blocked"
    assert body["blockers"] == ["0 of 20 paper trading days completed without a risk breach"]

    with SqliteState(settings.state.path) as state:
        state.execute(
            "UPDATE subscriptions SET paper_days_completed = 20 WHERE id = ?", [sub["id"]]
        )
    ok = client.patch(url, json={"mode": "auto", "reason": "ready"}, headers=alice["headers"])
    assert ok.status_code == 200, ok.text
    assert ok.json()["mode"] == "auto" and ok.json()["auto_blockers"] == []
