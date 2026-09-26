"""Console follow-ups (roadmap 11.1, 11.2, 11.6): the go-live route, order
rejection reasons, typed tick summary keys and the Studio capability flag."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from stonks.api import create_app
from stonks.app.golive import GoLiveService
from stonks.app.tick_summary import TickSummary
from stonks.config import GoLivePolicy
from stonks.store.state import SqliteState
from tests.integration.app.test_api import AUTH, LOOPBACK


@pytest.fixture
def app(settings, seeded, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    return create_app(settings, source_factory=lambda: fake_source)


@pytest.fixture
def client(app):
    with TestClient(app, client=LOOPBACK) as c:
        yield c


# ---- 11.1 go-live -------------------------------------------------------------


def test_golive_service_reports_every_check(services, seeded):
    view = GoLiveService(services.context).check(seeded["active_id"])
    assert view.strategy_id == seeded["active_id"]
    assert view.status == "active"
    assert view.source == "portfolio"
    names = [c.name for c in view.checks]
    assert names == ["status", "min_days", "max_drawdown", "max_drift", "min_trades", "survival"]
    # One paper day and no oos CAGR: the gate cannot pass yet.
    assert view.passed is False
    min_days = next(c for c in view.checks if c.name == "min_days")
    assert min_days.value == 1
    assert min_days.limit == GoLivePolicy().min_days
    assert min_days.passed is False
    assert view.policy == GoLivePolicy()


def test_golive_route_returns_typed_report(client, seeded):
    resp = client.get(f"/api/strategies/{seeded['shadow_id']}/golive", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["strategy_id"] == seeded["shadow_id"]
    assert body["source"] == "shadow"
    assert body["passed"] is False
    status = body["checks"][0]
    assert status == {
        "name": "status",
        "passed": True,
        "value": None,
        "limit": None,
        "detail": "shadow: paper period from shadow P&L",
    }
    survival = next(c for c in body["checks"] if c["name"] == "survival")
    assert survival["passed"] is False
    assert survival["detail"] == "no survival reports"


def test_golive_route_accepts_since(client, seeded):
    resp = client.get(
        f"/api/strategies/{seeded['active_id']}/golive", params={"since": "2030-01-01"}
    )
    assert resp.status_code == 200
    min_days = next(c for c in resp.json()["checks"] if c["name"] == "min_days")
    assert min_days["value"] == 0


def test_golive_route_unknown_strategy_is_404(client):
    resp = client.get("/api/strategies/nope/golive")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")


def test_golive_schema_names_are_an_enum(client):
    schema = client.get("/openapi.json").json()["components"]["schemas"]
    assert "GoLiveReport" in schema
    names = schema["GoLiveCheckView"]["properties"]["name"]
    assert set(names["enum"]) == {
        "status",
        "min_days",
        "max_drawdown",
        "max_drift",
        "min_trades",
        "survival",
    }


# ---- 11.2 order rejection reasons + typed tick summary ------------------------


def test_orders_carry_status_reason(client, settings, seeded):
    with SqliteState(settings.state.path) as state:
        state.execute(
            "UPDATE orders SET status = 'rejected', status_reason = 'insufficient buying power'"
        )
    items = client.get("/api/orders").json()["items"]
    assert items
    assert all(o["status_reason"] == "insufficient buying power" for o in items)


def test_orders_status_reason_defaults_to_null(client):
    items = client.get("/api/orders").json()["items"]
    assert items and all(o["status_reason"] is None for o in items)


def test_tick_summary_types_exit_and_stale_keys():
    summary = TickSummary.model_validate(
        {"reason": "no_candidates", "exit_strategy_id": "s1", "stale_buys_dropped": ["A.US"]}
    )
    assert summary.exit_strategy_id == "s1"
    assert summary.stale_buys_dropped == ["A.US"]
    assert TickSummary().stale_buys_dropped == []


def test_tick_route_exposes_stale_buys_dropped(client, settings, seeded):
    with SqliteState(settings.state.path) as state:
        state.execute(
            "UPDATE tick_runs SET summary_json = ? WHERE id = ?",
            [
                json.dumps({"exit_strategy_id": "x", "stale_buys_dropped": ["B.US"]}),
                seeded["tick_id"],
            ],
        )
    summary = client.get(f"/api/ticks/{seeded['tick_id']}").json()["summary"]
    assert summary["exit_strategy_id"] == "x"
    assert summary["stale_buys_dropped"] == ["B.US"]
    schema = client.get("/openapi.json").json()["components"]["schemas"]["TickSummary"]
    assert {"exit_strategy_id", "stale_buys_dropped"} <= set(schema["properties"])


# ---- 11.6 studio capabilities -------------------------------------------------


def test_studio_capabilities_reports_code_flag_off(client):
    resp = client.get("/api/studio/capabilities")
    assert resp.status_code == 200
    assert resp.json() == {"code_strategies": False}


def test_studio_capabilities_reports_code_flag_on(settings, seeded, fake_source):
    settings.api = settings.api.model_copy(
        update={"allow_code_strategies": True, "allowed_hosts": ["testserver"]}
    )
    app = create_app(settings, source_factory=lambda: fake_source)
    with TestClient(app, client=LOOPBACK) as c:
        assert c.get("/api/studio/capabilities").json() == {"code_strategies": True}
