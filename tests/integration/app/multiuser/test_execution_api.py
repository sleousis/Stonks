"""Execution algos and the rebalancing planner over the API (roadmap 23.16):
your own portfolios only, a plan sends nothing, a confirm writes tickets
that still wait for a fresh second factor."""

from __future__ import annotations

from stonks.accounts import PortfolioRepository, Scope
from stonks.store.state import SqliteState


def _portfolio(settings, person: dict) -> str:
    with SqliteState(settings.state.path) as state:
        scope = Scope(user_id=person["id"], role=person["role"])
        return PortfolioRepository(state).create(scope, name="Growth", initial_cash=10_000.0).id


def test_the_algo_catalog_lists_three_algos(client, people):
    r = client.get("/api/execution/algos", headers=people["alice"]["headers"])
    assert r.status_code == 200, r.text
    names = [a["name"] for a in r.json()["items"]]
    assert names == ["adaptive", "twap", "vwap"]


def test_set_list_and_clear_a_portfolio_algo(client, settings, people):
    pf = _portfolio(settings, people["alice"])
    alice = people["alice"]["headers"]
    url = f"/api/portfolios/{pf}/execution-algos"
    assert client.get(url, headers=alice).json() == {"items": []}
    put = client.put(url, json={"algo": "vwap", "params": {"end_minutes": 120}}, headers=alice)
    assert put.status_code == 200, put.text
    assert put.json()["params"]["max_participation"] == 0.1
    bad = client.put(url, json={"algo": "vwap", "params": {"slices": 0}}, headers=alice)
    assert bad.status_code == 422
    assert [s["algo"] for s in client.get(url, headers=alice).json()["items"]] == ["vwap"]
    # another person's portfolio reads as missing
    assert client.get(url, headers=people["bob"]["headers"]).status_code == 404
    assert client.delete(url, headers=alice).status_code == 204
    assert client.get(url, headers=alice).json() == {"items": []}
    parents = client.get(f"/api/portfolios/{pf}/algo-parents", headers=alice)
    assert parents.status_code == 200 and parents.json() == {"items": []}
    with SqliteState(settings.state.path) as state:
        actions = [r["action"] for r in state.sql("SELECT action FROM audit_log")]
    assert "execution_algo.set" in actions and "execution_algo.clear" in actions


def test_a_plan_shows_trades_and_sends_nothing(client, settings, people):
    pf = _portfolio(settings, people["alice"])
    alice = people["alice"]["headers"]
    body = {
        "portfolio_id": pf,
        "source": "targets",
        "targets": [{"ticker": "UP.US", "weight": 0.5}],
    }
    r = client.post("/api/planner/plan", json=body, headers=alice)
    assert r.status_code == 200, r.text
    plan = r.json()
    [line] = [x for x in plan["lines"] if x["side"]]
    assert line["ticker"] == "UP.US" and line["side"] == "buy"
    assert line["quantity"] == int(line["quantity"]) and line["quantity"] > 0
    assert plan["turnover"] > 0 and plan["total_cost"] > 0  # realistic costs
    with SqliteState(settings.state.path) as state:
        assert state.sql("SELECT COUNT(*) FROM order_tickets")[0][0] == 0
    assert (
        client.post("/api/planner/plan", json=body, headers=people["bob"]["headers"]).status_code
        == 404
    )


def test_confirm_writes_tickets_that_wait_for_approval(client, settings, people):
    pf = _portfolio(settings, people["alice"])
    alice = people["alice"]["headers"]
    body = {"portfolio_id": pf, "source": "targets", "reason": "quarterly rebalance",
            "targets": [{"ticker": "UP.US", "weight": 0.4}, {"ticker": "DOWN.US", "weight": 0.4}]}  # fmt: skip
    r = client.post("/api/planner/confirm", json=body, headers=alice)
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["written"] == 2 and len(first["ticket_ids"]) == 2
    tickets = client.get(f"/api/tickets?portfolio_id={pf}", headers=alice).json()["items"]
    assert {t["status"] for t in tickets} == {"awaiting_approval"}
    assert {t["hold"] for t in tickets} == {"approve_mode"}
    assert tickets[0]["reason"]["source"] == "planner"
    # the same plan again writes nothing new
    again = client.post("/api/planner/confirm", json=body, headers=alice).json()
    assert again["written"] == 0 and again["ticket_ids"] == first["ticket_ids"]


def test_plan_requests_are_checked(client, settings, people):
    pf = _portfolio(settings, people["alice"])
    alice = people["alice"]["headers"]
    over = {"portfolio_id": pf, "source": "targets",
            "targets": [{"ticker": "UP.US", "weight": 0.7}, {"ticker": "DOWN.US", "weight": 0.7}]}  # fmt: skip
    assert client.post("/api/planner/plan", json=over, headers=alice).status_code == 422
    no_strategy = {"portfolio_id": pf, "source": "strategy"}
    assert client.post("/api/planner/plan", json=no_strategy, headers=alice).status_code == 422
    unknown = {"portfolio_id": pf, "source": "strategy", "strategy_id": "nope"}
    assert client.post("/api/planner/plan", json=unknown, headers=alice).status_code == 404
    empty = {"portfolio_id": pf, "source": "targets", "reason": "nothing", "targets": []}
    assert client.post("/api/planner/confirm", json=empty, headers=alice).status_code == 422
