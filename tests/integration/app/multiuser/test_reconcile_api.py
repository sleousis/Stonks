"""Reconciliation reports over the API (roadmap 19.5): your own portfolios
only, newest first, every item and the owner's external holdings."""

from __future__ import annotations

from datetime import timedelta

from stonks.accounts import PortfolioRepository, Scope
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.clock import FakeClock
from stonks.core.types import Portfolio
from stonks.production.live.checks import run_check
from stonks.store.state import SqliteState
from tests.fakes.ib_gateway import T0


def _portfolio(settings, person: dict, name: str) -> str:
    with SqliteState(settings.state.path) as state:
        scope = Scope(user_id=person["id"], role=person["role"])
        return PortfolioRepository(state).create(scope, name=name, kind="broker").id


def _check(settings, pid: str, positions: dict[str, float], kind="sod", at=T0) -> str:
    with SqliteState(settings.state.path) as state:
        broker = SimulatedBroker(portfolio=Portfolio(cash=0.0, positions=positions))
        result = run_check(state, broker, pid, kind, clock=FakeClock(at), publish=lambda e: None)
    return result.report.id


def test_reports_list_and_read(client, settings, people):
    alice = people["alice"]["headers"]
    pid = _portfolio(settings, people["alice"], "Live")
    first = _check(settings, pid, {"MSFT.US": 3.0})
    second = _check(settings, pid, {}, kind="eod", at=T0 + timedelta(hours=7))

    listed = client.get("/api/reconcile/reports", headers=alice)
    assert listed.status_code == 200, listed.text
    assert [r["id"] for r in listed.json()["items"]] == [second, first]
    one = client.get(f"/api/reconcile/reports?portfolio_id={pid}&limit=1", headers=alice)
    assert [r["id"] for r in one.json()["items"]] == [second]

    got = client.get(f"/api/reconcile/reports/{first}", headers=alice)
    assert got.status_code == 200
    view = got.json()
    assert (view["kind"], view["status"], view["portfolio_id"]) == ("sod", "clean", pid)
    assert view["external"] == {"positions": {"MSFT.US": 3.0}, "orders": []}
    assert view["items"] == [] and view["halt_id"] is None and view["paused"] == []


def test_another_persons_reports_read_as_missing(client, settings, people):
    pid = _portfolio(settings, people["alice"], "Live")
    report = _check(settings, pid, {})
    for who in ("bob", "ada"):
        headers = people[who]["headers"]
        assert client.get(f"/api/reconcile/reports/{report}", headers=headers).status_code == 404
        mine = client.get(f"/api/reconcile/reports?portfolio_id={pid}", headers=headers)
        assert mine.status_code == 404
        assert client.get("/api/reconcile/reports", headers=headers).json()["items"] == []
    missing = client.get("/api/reconcile/reports/rec_nope", headers=people["alice"]["headers"])
    assert missing.status_code == 404
