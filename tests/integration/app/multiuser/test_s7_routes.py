"""Roadmap step S7, the routes the console still missed: portfolios,
subscriptions, trading modes, market sessions, backups on disk, machine
error codes and channel defaults."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.integration.app.multiuser.conftest import BASE
from tests.integration.app.test_api import AUTH, REMOTE
from tests.integration.auth.helpers import PASSWORD, add_user

# ---- problem details: machine codes ---------------------------------------------


def test_problem_details_carry_a_machine_code(client, people):
    missing = client.get("/api/jobs/job_nope", headers=people["alice"]["headers"])
    assert missing.status_code == 404 and missing.json()["code"] == "not_found"
    unauth = client.get("/api/jobs")
    assert unauth.status_code == 401 and unauth.json()["code"] == "not_authenticated"
    bad = client.get("/api/alerts", params={"level": "bogus"}, headers=AUTH)
    assert bad.status_code == 422 and bad.json()["code"] == "validation_failed"
    forbidden = client.get("/api/auth/users", headers=people["alice"]["headers"])
    assert forbidden.status_code == 403 and forbidden.json()["code"] == "forbidden"


def test_step_up_refusal_has_its_code(client):
    resp = client.post(
        "/api/auth/users",
        json={
            "email": "x@example.com",
            "display_name": "X",
            "role": "trader",
            "password": PASSWORD,
        },
        headers=AUTH,
    )
    body = resp.json()
    assert resp.status_code == 403 and body["code"] == "step_up_required"
    assert body["detail"].startswith("step_up_required")


@pytest.mark.parametrize("enrolled", [False, True])
def test_mfa_required_says_what_to_do_next(app, settings, auth, enrolled):
    import pyotp

    add_user(settings.state.path, "alice@example.com")
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        if enrolled:
            login = c.post(
                "/api/auth/login", json={"email": "alice@example.com", "password": PASSWORD}
            ).json()
            csrf = {"X-CSRF-Token": login["csrf_token"]}
            secret = c.post("/api/auth/mfa/enrol", headers=csrf).json()["secret"]
            c.post(
                "/api/auth/mfa/enrol/confirm",
                json={"code": pyotp.TOTP(secret).now()},
                headers=csrf,
            )
            c.cookies.clear()
        c.post("/api/auth/login", json={"email": "alice@example.com", "password": PASSWORD})
        resp = c.get("/api/auth/me")
    body = resp.json()
    assert resp.status_code == 401 and body["code"] == "mfa_required"
    assert body["next_step"] == ("verify" if enrolled else "enrol")


# ---- notification channel defaults ------------------------------------------------


def test_preferences_expose_each_channels_default(client, people):
    body = client.get("/api/notifications/preferences", headers=people["alice"]["headers"]).json()
    defaults = {d["channel"]: d for d in body["channel_defaults"]}
    assert set(defaults) == set(body["channels"])
    assert defaults["webpush"]["default_enabled"] is True
    assert defaults["webhook"]["default_enabled"] is False
    assert defaults["webhook"]["fallback"] is True
