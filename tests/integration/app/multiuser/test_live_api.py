"""A live portfolio's allocation and account profile over the API (roadmap
19.6, 19.7): your own portfolios only, changes need a fresh second factor
and are audited."""

from __future__ import annotations

from stonks.accounts import PortfolioRepository, Scope
from stonks.store.state import SqliteState
from tests.integration.app.stepup import allow_step_up


def _portfolio(settings, person: dict, name: str) -> str:
    with SqliteState(settings.state.path) as state:
        scope = Scope(user_id=person["id"], role=person["role"])
        return PortfolioRepository(state).create(scope, name=name, kind="broker").id


def test_allocation_starts_empty_and_needs_a_step_up(app, client, settings, people):
    alice = people["alice"]["headers"]
    pid = _portfolio(settings, people["alice"], "Live")
    url = f"/api/portfolios/{pid}/live/allocation"
    got = client.get(url, headers=alice)
    assert got.status_code == 200 and got.json()["amount"] is None
    body = {"amount": 2500, "currency": "usd", "reason": "first slice"}
    refused = client.put(url, json=body, headers=alice)
    assert refused.status_code == 403 and refused.json()["code"] == "step_up_required"

    allow_step_up(app)
    ok = client.put(url, json=body, headers=alice)
    assert ok.status_code == 200, ok.text
    view = ok.json()
    assert (view["amount"], view["currency"], view["updated_by"]) == (
        2500.0,
        "USD",
        f"user:{people['alice']['id']}",
    )
    assert client.put(url, json={**body, "amount": -1}, headers=alice).status_code == 422
    with SqliteState(settings.state.path) as state:
        rows = state.sql("SELECT actor FROM audit_log WHERE action = 'live.allocation_set'")
    assert [r["actor"] for r in rows] == [f"user:{people['alice']['id']}"]


def test_another_persons_portfolio_reads_as_missing(app, client, settings, people):
    pid = _portfolio(settings, people["alice"], "Live")
    allow_step_up(app)
    for who in ("bob", "ada"):
        headers = people[who]["headers"]
        assert (
            client.get(f"/api/portfolios/{pid}/live/allocation", headers=headers).status_code == 404
        )
        put = client.put(
            f"/api/portfolios/{pid}/live/allocation",
            json={"amount": 1, "currency": "USD", "reason": "x"},
            headers=headers,
        )
        assert put.status_code == 404
    viewer = client.put(
        f"/api/portfolios/{pid}/live/allocation",
        json={"amount": 1, "currency": "USD", "reason": "x"},
        headers=people["vic"]["headers"],
    )
    assert viewer.status_code == 403


def test_account_profile_round_trip(app, client, settings, people):
    alice = people["alice"]["headers"]
    pid = _portfolio(settings, people["alice"], "Live")
    url = f"/api/portfolios/{pid}/live/account-profile"
    assert client.get(url, headers=alice).status_code == 404
    allow_step_up(app)
    short_on_cash = {"jurisdiction": "us", "account_type": "cash", "allow_short": True}
    assert client.put(url, json=short_on_cash, headers=alice).status_code == 422
    ok = client.put(url, json={"jurisdiction": "uk", "base_currency": "gbp"}, headers=alice)
    assert ok.status_code == 200, ok.text
    assert ok.json() == {
        "portfolio_id": pid,
        "jurisdiction": "uk",
        "account_type": "cash",
        "client_class": "retail",
        "base_currency": "GBP",
        "fx_policy": "refuse",
        "wash_sale_mode": "warn",
        "allow_short": False,
    }
    assert client.get(url, headers=alice).json()["jurisdiction"] == "uk"
