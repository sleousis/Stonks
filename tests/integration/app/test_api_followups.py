"""Console follow-ups (roadmap 11.1, 11.2, 11.6): the go-live route, order
rejection reasons, typed tick summary keys and the Studio capability flag."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from stonks.api import create_app
from stonks.app.golive import GoLiveService
from stonks.app.tick_summary import TickSummary
from stonks.config import GoLivePolicy
from stonks.store.state import SqliteState
from tests.integration.app.test_api import AUTH, LOOPBACK

# ---- 11.1 go-live -------------------------------------------------------------

LEGACY_CHECKS = ["status", "min_days", "max_drawdown", "max_drift", "min_trades", "survival"]
INCUBATION_CHECKS = [
    "within_mc_band",
    "quit_rule",
    "promotion_preset",
    "nonzero_costs",
    "hypothesis_recorded",
    "backtest_min_trades",
]


def test_golive_service_reports_every_check(services, seeded):
    view = GoLiveService(services.context).check(seeded["active_id"])
    assert view.strategy_id == seeded["active_id"]
    assert view.status == "active"
    assert view.source == "portfolio"
    names = [c.name for c in view.checks]
    assert names == [*LEGACY_CHECKS, *INCUBATION_CHECKS]  # [golive] incubation = true
    # One paper day and no oos CAGR: the gate cannot pass yet.
    assert view.passed is False
    min_days = next(c for c in view.checks if c.name == "min_days")
    assert min_days.value == 1
    # max(min_days, MinTRL of the oos Sharpe), capped: MinTRL here exceeds the cap
    assert min_days.limit == GoLivePolicy().min_trl_cap_days
    assert min_days.passed is False
    assert view.policy == GoLivePolicy()
    # promotion context for the reviewer: nothing recorded for this seed
    assert view.checklist.n_trials_class is None
    assert view.checklist.dsr is None and view.checklist.pbo is None


def test_golive_service_legacy_policy_keeps_six_checks(services, seeded):
    services.context.settings.golive = GoLivePolicy(incubation=False)
    view = GoLiveService(services.context).check(seeded["active_id"])
    assert [c.name for c in view.checks] == LEGACY_CHECKS


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
        "detail": "Paper trading, measured on its paper trading results",
    }
    survival = next(c for c in body["checks"] if c["name"] == "survival")
    assert survival["passed"] is False
    assert survival["detail"] == "No robustness tests on record"


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
    assert set(names["enum"]) == {*LEGACY_CHECKS, *INCUBATION_CHECKS}
    checklist = schema["PromotionChecklistView"]["properties"]
    assert set(checklist) == {
        "n_trials_class",
        "dsr",
        "pbo",
        "excess_cagr",
        "premortem",
        "hypothesis",
        "min_capital",
        "lot_skipped_share",
    }


# ---- 11.2 order rejection reasons + typed tick summary ------------------------


def test_orders_carry_status_reason(client, settings, seeded):
    with SqliteState(settings.state.path) as state:
        state.execute(
            "UPDATE orders SET status = 'rejected', status_reason = 'insufficient buying power'"
        )
    items = client.get("/api/orders", headers=AUTH).json()["items"]
    assert items
    assert all(o["status_reason"] == "insufficient buying power" for o in items)


def test_orders_status_reason_defaults_to_null(client):
    items = client.get("/api/orders", headers=AUTH).json()["items"]
    assert items and all(o["status_reason"] is None for o in items)


def test_orders_carry_the_fine_state(client, settings, seeded):
    with SqliteState(settings.state.path) as state:
        state.execute("UPDATE orders SET status = 'pending', state = 'unknown'")
    items = client.get("/api/orders", headers=AUTH).json()["items"]
    assert items and all(o["state"] == "unknown" for o in items)


def test_orders_say_which_are_protective_stops(client, settings, seeded):
    items = client.get("/api/orders", headers=AUTH).json()["items"]
    assert items and all(o["protective"] is False and o["stop_price"] is None for o in items)
    with SqliteState(settings.state.path) as state:
        state.execute(
            "UPDATE orders SET order_type = 'stop', stop_price = 90.5, time_in_force = 'gtc',"
            " protective = 1"
        )
    items = client.get("/api/orders", headers=AUTH).json()["items"]
    assert all(
        (o["protective"], o["stop_price"], o["time_in_force"]) == (True, 90.5, "gtc") for o in items
    )


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
    summary = client.get(f"/api/ticks/{seeded['tick_id']}", headers=AUTH).json()["summary"]
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
