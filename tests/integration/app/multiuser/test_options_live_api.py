"""A portfolio's live options state and approval level over the API (roadmap
17.8): off by default, the level needs a fresh second factor and a
reason, and it is audited."""

from __future__ import annotations

from stonks.accounts import PortfolioRepository, Scope
from stonks.store.state import SqliteState
from tests.integration.app.stepup import allow_step_up


def _portfolio(settings, person: dict, name: str) -> str:
    with SqliteState(settings.state.path) as state:
        scope = Scope(user_id=person["id"], role=person["role"])
        return PortfolioRepository(state).create(scope, name=name, kind="broker").id


def test_options_live_is_off_with_every_reason(client, settings, people):
    alice = people["alice"]["headers"]
    pid = _portfolio(settings, people["alice"], "Live")
    got = client.get(f"/api/portfolios/{pid}/live/options", headers=alice)
    assert got.status_code == 200, got.text
    view = got.json()
    assert (view["enabled"], view["allowed"], view["level"], view["stage"]) == (
        False,
        False,
        "none",
        "sim_paper",
    )
    assert len(view["reasons"]) == 3
    assert [lv["level"] for lv in view["levels"]] == ["none", "covered", "spreads", "naked"]


def test_the_level_needs_a_step_up_and_is_audited(app, client, settings, people):
    alice = people["alice"]["headers"]
    pid = _portfolio(settings, people["alice"], "Live")
    url = f"/api/portfolios/{pid}/live/options/approval"
    body = {"level": "covered", "reason": "IBKR granted level 2"}
    refused = client.put(url, json=body, headers=alice)
    assert refused.status_code == 403 and refused.json()["code"] == "step_up_required"
    allow_step_up(app)
    ok = client.put(url, json=body, headers=alice)
    assert ok.status_code == 200, ok.text
    view = ok.json()
    assert view["level"] == "covered" and view["reason"] == "IBKR granted level 2"
    assert view["allowed"] is False  # the switch and the stage still say no
    assert (
        client.put(url, json={"level": "level9", "reason": "x"}, headers=alice).status_code == 422
    )
    assert (
        client.put(url, json={"level": "spreads", "reason": ""}, headers=alice).status_code == 422
    )
    with SqliteState(settings.state.path) as state:
        rows = state.sql("SELECT actor FROM audit_log WHERE action = 'options.approval_set'")
    assert [r["actor"] for r in rows] == [f"user:{people['alice']['id']}"]


def test_another_persons_portfolio_reads_as_missing(app, client, settings, people):
    pid = _portfolio(settings, people["alice"], "Live")
    allow_step_up(app)
    bob = people["bob"]["headers"]
    assert client.get(f"/api/portfolios/{pid}/live/options", headers=bob).status_code == 404
    put = client.put(
        f"/api/portfolios/{pid}/live/options/approval",
        json={"level": "naked", "reason": "x"},
        headers=bob,
    )
    assert put.status_code == 404
    viewer = client.put(
        f"/api/portfolios/{pid}/live/options/approval",
        json={"level": "naked", "reason": "x"},
        headers=people["vic"]["headers"],
    )
    assert viewer.status_code == 403
