"""System settings in the admin console (complexity audit F61): safe,
non-secret keys an admin changes through the API, validated, audited, and in
effect without a restart for the blocks that read settings per run."""

from __future__ import annotations

from stonks.store.state import SqliteState
from tests.integration.app.stepup import allow_step_up

URL = "/api/settings/system"
CAP = "production.risk.max_weight_per_ticker"


def test_only_admins_read_the_system_settings(client, people):
    assert client.get(URL, headers=people["alice"]["headers"]).status_code == 403
    body = client.get(URL, headers=people["ada"]["headers"])
    assert body.status_code == 200, body.text
    items = {s["key"]: s for s in body.json()["items"]}
    cap = items[CAP]
    assert (cap["group"], cap["applies"], cap["overridden"]) == ("risk", "next_run", False)
    assert cap["value"] == cap["default"]
    assert items["scheduler.jobs.tick.enabled"]["applies"] == "restart"
    text = body.text.lower()
    for secret in ("api_key", "webhook.url", "secret", "password", "token", "state.path"):
        assert secret not in text, secret


def test_a_change_needs_a_fresh_second_factor(client, people):
    ada = people["ada"]["headers"]
    body = {"value": 0.1, "reason": "tighter for new traders"}
    refused = client.put(f"{URL}/{CAP}", json=body, headers=ada)
    assert refused.status_code == 403 and refused.json()["code"] == "step_up_required"
    trader = client.put(f"{URL}/{CAP}", json=body, headers=people["alice"]["headers"])
    assert trader.status_code == 403


def test_a_change_is_validated_stored_audited_and_used_at_once(app, client, settings, people):
    allow_step_up(app)
    ada = people["ada"]["headers"]
    bad = client.put(f"{URL}/{CAP}", json={"value": 1.5, "reason": "oops"}, headers=ada)
    assert bad.status_code == 422, bad.text
    ok = client.put(f"{URL}/{CAP}", json={"value": 0.1, "reason": "tighter"}, headers=ada)
    assert ok.status_code == 200, ok.text
    view = ok.json()
    assert (view["value"], view["overridden"], view["reason"]) == (0.1, True, "tighter")
    assert view["default"] == 1.0 and view["updated_by"].startswith("user:")
    # the risk policy the next run reads, with no restart
    assert client.get("/api/risk/policy", headers=ada).json()["max_weight_per_ticker"] == 0.1
    assert app.state.services.context.settings.production.risk.max_weight_per_ticker == 0.1
    with SqliteState(settings.state.path) as state:
        audit = state.sql(
            "SELECT action, target_id FROM audit_log WHERE target_kind = 'setting' ORDER BY id"
        )
    assert [(r["action"], r["target_id"]) for r in audit] == [("settings.override", CAP)]

    reset = client.post(f"{URL}/{CAP}/reset", json={"reason": "back to TOML"}, headers=ada)
    assert reset.status_code == 200, reset.text
    assert (reset.json()["value"], reset.json()["overridden"]) == (1.0, False)
    assert client.get("/api/risk/policy", headers=ada).json()["max_weight_per_ticker"] == 1.0


def test_secrets_and_unknown_keys_are_not_editable(app, client, people):
    allow_step_up(app)
    ada = people["ada"]["headers"]
    for key in ("sources.eodhd.api_key", "notify.webhook.url", "state.path", "nope.nothing"):
        r = client.put(f"{URL}/{key}", json={"value": "x", "reason": "try it"}, headers=ada)
        assert r.status_code == 404, (key, r.status_code)
        assert client.get(f"{URL}/{key}", headers=ada).status_code == 404


def test_a_schedule_switch_and_a_nested_rule(app, client, people):
    allow_step_up(app)
    ada = people["ada"]["headers"]
    off = client.put(
        f"{URL}/scheduler.jobs.tick.enabled",
        json={"value": False, "reason": "holiday"},
        headers=ada,
    )
    assert off.status_code == 200 and off.json()["value"] is False
    breaker = client.put(
        f"{URL}/production.risk.rules.circuit_breaker.max_month_loss",
        json={"value": 0.05, "reason": "tighter breaker"},
        headers=ada,
    )
    assert breaker.status_code == 200, breaker.text
    rules = client.get("/api/risk/policy", headers=ada).json()["rules"]
    assert rules["circuit_breaker"]["max_month_loss"] == 0.05
