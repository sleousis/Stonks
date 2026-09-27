"""Deposits and withdrawals through the API (roadmap 20.5): they move a
simulated book's cash, and a deposit never shows as return."""

from __future__ import annotations

import pytest

from stonks.accounts import PortfolioRepository, Role, Scope
from stonks.store.state import SqliteState


def _book(settings, user_id: str, kind: str = "simulated") -> str:
    with SqliteState(settings.state.path) as state:
        return (
            PortfolioRepository(state)
            .create(
                Scope(user_id=user_id, role=Role.TRADER),
                name=f"Book {kind}",
                kind=kind,  # type: ignore[arg-type]
                initial_cash=10_000.0,
            )
            .id
        )


def _flow(client, pid, headers, **body):
    return client.post(f"/api/portfolios/{pid}/cash-flows", json=body, headers=headers)


def test_a_deposit_mid_period_is_not_return(client, people, settings):
    alice = people["alice"]
    pid = _book(settings, alice["id"])
    h = alice["headers"]
    first = _flow(client, pid, h, kind="deposit", amount=5_000, flow_date="2026-03-02")
    assert first.status_code == 201, first.text
    second = _flow(client, pid, h, kind="deposit", amount=5_000, flow_date="2026-03-16")
    assert second.status_code == 201
    listed = client.get(f"/api/portfolios/{pid}/cash-flows", headers=h).json()
    assert [f["amount"] for f in listed["items"]] == [5_000, 5_000]
    assert all(f["source"] == "manual" for f in listed["items"])

    pnl = client.get("/api/pnl", params={"portfolio_id": pid}, headers=h).json()
    values = [r["total_value"] for r in pnl["rows"]]
    assert values == [15_000, 20_000]
    assert pnl["rows"][-1]["cumulative_return"] == pytest.approx(1 / 3)  # plain change
    assert pnl["twr"] == pytest.approx(0.0) and pnl["net_flows"] == 5_000
    assert pnl["mwr"] == pytest.approx(0.0, abs=1e-9)

    insights = client.get("/api/insights", params={"portfolio_id": pid}, headers=h).json()
    inception = next(r for r in insights["pnl"] if r["period"] == "inception")
    assert inception["change"] == 5_000 and inception["twr"] == pytest.approx(0.0)
    assert insights["net_flows"] == 5_000
    assert insights["cash"] == 20_000


def test_withdrawals_and_refusals(client, people, settings):
    alice, bob, vic = people["alice"], people["bob"], people["vic"]
    pid = _book(settings, alice["id"])
    h = alice["headers"]
    too_much = _flow(client, pid, h, kind="withdrawal", amount=50_000, flow_date="2026-03-02")
    assert too_much.status_code == 409
    ok = _flow(client, pid, h, kind="withdrawal", amount=1_000, flow_date="2026-03-02")
    assert ok.status_code == 201
    before = _flow(client, pid, h, kind="deposit", amount=1, flow_date="2026-03-01")
    assert before.status_code == 409  # earlier than the latest snapshot
    future = _flow(client, pid, h, kind="deposit", amount=1, flow_date="2999-01-01")
    assert future.status_code == 409
    assert _flow(client, pid, h, kind="deposit", amount=-5).status_code == 422
    for who in (bob, people["ada"]):
        assert _flow(client, pid, who["headers"], kind="deposit", amount=1).status_code == 404
        r = client.get(f"/api/portfolios/{pid}/cash-flows", headers=who["headers"])
        assert r.status_code == 404
    assert _flow(client, pid, vic["headers"], kind="deposit", amount=1).status_code == 403
    broker = _book(settings, alice["id"], kind="broker")
    assert _flow(client, broker, h, kind="deposit", amount=1).status_code == 409
