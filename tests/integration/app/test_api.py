"""REST API: routes, auth, errors, pagination, jobs + SSE, CORS, static UI."""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from stonks.api import create_app
from tests.integration.app.conftest import API_TOKEN, AUTH, LOOPBACK, REMOTE

__all__ = ["API_TOKEN", "AUTH", "BAH", "LOOPBACK", "REMOTE"]

BAH = "stonks.strategies.examples.buy_and_hold:BuyAndHold"


def _wait_job(client: TestClient, job_id: str, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


# ---- health + auth ----------------------------------------------------------


def test_health_is_open_even_remotely(remote):
    resp = remote.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_reads_open_on_loopback(client):
    assert client.get("/api/strategies").status_code == 200


def test_reads_need_token_from_non_loopback(remote):
    resp = remote.get("/api/strategies")
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.headers["www-authenticate"] == "Bearer"
    assert remote.get("/api/strategies", headers=AUTH).status_code == 200


def test_reads_need_token_when_open_reads_disabled(settings, seeded, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    settings.api.open_reads_on_loopback = False
    app = create_app(settings, source_factory=lambda: fake_source)
    with TestClient(app, client=LOOPBACK) as c:
        assert c.get("/api/strategies").status_code == 401
        assert c.get("/api/strategies", headers=AUTH).status_code == 200


def _operations(app):
    for path, item in app.openapi()["paths"].items():
        for method, op in item.items():
            yield method.upper(), path, op


def test_every_mutating_route_requires_token(app, client):
    # Sign-in and sign-out check their own credentials (tests/.../test_api_auth.py).
    public = {"/api/auth/login", "/api/auth/logout"}
    mutating = [
        (m, p)
        for m, p, _ in _operations(app)
        if m not in {"GET", "HEAD", "OPTIONS"} and p not in public
    ]
    assert len(mutating) >= 8
    for method, path in mutating:
        url = path.replace("{strategy_id}", "bah_shadow").replace("{job_id}", "job_x")
        resp = client.request(method, url, json={})
        assert resp.status_code == 401, (method, path, resp.status_code)
        bad = client.request(method, url, json={}, headers={"Authorization": "Bearer nope"})
        assert bad.status_code == 401, (method, path)


def test_mutating_routes_fail_closed_without_configured_token(settings, seeded, monkeypatch):
    monkeypatch.delenv("STONKS_API_TOKEN")
    settings.api = settings.api.model_copy(update={"token": None, "allowed_hosts": ["testserver"]})
    app = create_app(settings)
    with TestClient(app, client=LOOPBACK) as c:
        resp = c.post("/api/strategies/bah_shadow/promote", headers=AUTH)
        assert resp.status_code == 503
        assert "STONKS_API_TOKEN" in resp.json()["detail"]


def test_unknown_host_header_rejected(client):
    resp = client.get("/api/health", headers={"Host": "evil.example"})
    assert resp.status_code == 400


# ---- errors + pagination ----------------------------------------------------


def test_not_found_is_problem_details(client):
    resp = client.get("/api/strategies/missing")
    assert resp.status_code == 404
    body = resp.json()
    assert body["status"] == 404
    assert body["title"] == "Not found"
    assert "missing" in body["detail"]
    assert body["instance"] == "/api/strategies/missing"


def test_validation_error_is_problem_details(client):
    resp = client.get("/api/strategies", params={"limit": 0})
    assert resp.status_code == 422
    body = resp.json()
    assert body["status"] == 422
    assert body["errors"][0]["loc"][-1] == "limit"


def test_limit_above_max_is_rejected(client, settings):
    resp = client.get("/api/jobs", params={"limit": settings.api.max_page_size + 1})
    assert resp.status_code == 422


def test_unknown_api_route_is_problem_404(client):
    resp = client.get("/api/nope")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")


# ---- read routes ------------------------------------------------------------


def test_portfolio_routes(client):
    body = client.get("/api/portfolio", headers=AUTH).json()
    assert body["positions"][0]["ticker"] == "UP.US"
    snaps = client.get("/api/portfolio/snapshots", headers=AUTH).json()
    assert snaps["total"] == 1


def test_strategy_routes(client, seeded):
    page = client.get("/api/strategies", params={"status": "active"}).json()
    assert [s["id"] for s in page["items"]] == [seeded["active_id"]]
    assert page["limit"] == 50 and page["offset"] == 0
    detail = client.get(f"/api/strategies/{seeded['active_id']}").json()
    assert detail["survival_reports"][0]["test_id"] == "oos"
    assert client.get("/api/strategies", params={"status": "bogus"}).status_code == 422


def test_strategy_status_routes(client, seeded):
    """Status routes go through the governed service (BL-24): promotion
    needs a passing go-live check, demotion a reason. Without a body both
    are refused and nothing changes (see test_governance_wiring)."""
    sid = seeded["shadow_id"]
    assert client.post(f"/api/strategies/{sid}/promote", headers=AUTH).status_code == 409
    for action in ("retire", "shadow"):
        resp = client.post(f"/api/strategies/{seeded['active_id']}/{action}", headers=AUTH)
        assert resp.status_code == 422
    assert client.get(f"/api/strategies/{sid}").json()["status"] == "shadow"
    assert client.get(f"/api/strategies/{seeded['active_id']}").json()["status"] == "active"
    assert client.post("/api/strategies/missing/promote", headers=AUTH).status_code == 404


def test_market_routes(client):
    inst = client.get("/api/market/instruments", params={"q": "flat"}).json()
    assert [i["id"] for i in inst["items"]] == ["FLAT.US"]
    bars = client.get(
        "/api/market/bars",
        params={"ticker": "UP.US", "interval": "1d", "start": "2026-03-30", "end": "2026-04-01"},
    ).json()
    assert len(bars["bars"]) == 3
    assert (
        client.get("/api/market/bars", params={"ticker": "UP.US", "interval": "9q"}).status_code
        == 422
    )
    cov = client.get("/api/market/coverage").json()
    assert cov["total"] == 3


def test_orders_ticks_routes(client, seeded):
    orders = client.get("/api/orders", params={"tick_id": seeded["tick_id"]}, headers=AUTH).json()
    assert orders["total"] == 1
    fills = client.get(
        "/api/orders/fills", params={"tick_id": seeded["tick_id"]}, headers=AUTH
    ).json()
    assert fills["total"] == 1
    assert fills["items"][0]["side"] == orders["items"][0]["side"]
    ticks = client.get("/api/ticks", headers=AUTH).json()
    assert ticks["total"] == 1
    tick = client.get(f"/api/ticks/{seeded['tick_id']}", headers=AUTH).json()
    assert len(tick["orders"]) == 1
    assert client.get("/api/ticks/tick_missing", headers=AUTH).status_code == 404


def test_catalog_routes(client):
    strategies = client.get("/api/catalog/strategies").json()
    assert BAH in [s["class_path"] for s in strategies]
    assert "1d" in [i["code"] for i in client.get("/api/catalog/intervals").json()]
    assert client.get("/api/catalog/asset-classes").json() == [
        "equity",
        "crypto",
        "commodity",
        "bond",
    ]


def test_ingest_runs_route(client):
    assert client.get("/api/ingest/runs").json()["total"] == 0


# ---- jobs -------------------------------------------------------------------


def _backtest_body(**overrides) -> dict:
    body = {
        "strategy": {"class_path": BAH, "params": {"ticker": "UP.US"}},
        "universe": ["UP.US"],
        "start": "2025-10-01",
        "end": "2026-04-01",
    }
    body.update(overrides)
    return body


def test_backtest_job_lifecycle(client):
    resp = client.post("/api/lab/backtests", json=_backtest_body(), headers=AUTH)
    assert resp.status_code == 202
    job = resp.json()
    assert job["kind"] == "backtest"
    assert resp.headers["location"] == f"/api/jobs/{job['id']}"
    done = _wait_job(client, job["id"])
    assert done["status"] == "succeeded", done["error"]
    assert done["result"]["final_return"] > 0.5
    listed = client.get("/api/jobs", params={"kind": "backtest"}).json()
    assert listed["total"] == 1


def test_backtest_bad_strategy_is_422_and_not_queued(client):
    resp = client.post(
        "/api/lab/backtests",
        json=_backtest_body(strategy={"class_path": "os:system"}),
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert client.get("/api/jobs").json()["total"] == 0


def test_backtest_body_validation(client):
    resp = client.post(
        "/api/lab/backtests",
        json=_backtest_body(start="2026-05-01", end="2026-01-01"),
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_lab_run_job(client):
    body = _backtest_body(
        strategy={"class_path": "stonks.strategies.examples.momentum:Momentum"},
        universe=["UP.US", "DOWN.US"],
        budget=2,
        survival_tests=["oos"],
    )
    resp = client.post("/api/lab/runs", json=body, headers=AUTH)
    assert resp.status_code == 202
    done = _wait_job(client, resp.json()["id"], timeout=120)
    assert done["status"] == "succeeded", done["error"]
    assert done["result"]["verdict"] in ("pass", "fail")


def test_ingest_job(client):
    resp = client.post(
        "/api/ingest/runs", json={"kind": "prices", "tickers": ["NEW.US"]}, headers=AUTH
    )
    assert resp.status_code == 202
    done = _wait_job(client, resp.json()["id"])
    assert done["status"] == "succeeded", done["error"]
    assert client.get("/api/ingest/runs").json()["total"] == 1


def test_tick_job(client):
    resp = client.post(
        "/api/ticks",
        json={"dry_run": True, "tickers": ["UP.US"], "as_of": "2026-03-27"},
        headers=AUTH,
    )
    assert resp.status_code == 202
    done = _wait_job(client, resp.json()["id"])
    assert done["status"] == "succeeded", done["error"]
    assert done["result"]["dry_run"] is True


def test_cancel_unknown_job_404_and_finished_job_409(client):
    assert client.post("/api/jobs/job_missing/cancel", headers=AUTH).status_code == 404
    job = client.post("/api/lab/backtests", json=_backtest_body(), headers=AUTH).json()
    _wait_job(client, job["id"])
    resp = client.post(f"/api/jobs/{job['id']}/cancel", headers=AUTH)
    assert resp.status_code == 409


def test_job_events_stream_until_terminal(client):
    job = client.post("/api/lab/backtests", json=_backtest_body(), headers=AUTH).json()
    events = []
    with client.stream("GET", f"/api/jobs/{job['id']}/events") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :]))
    assert events, "no events received"
    assert events[-1]["status"] == "succeeded"
    assert all(e["job_id"] == job["id"] for e in events)


def test_job_events_unknown_job_is_404(client):
    resp = client.get("/api/jobs/job_missing/events")
    assert resp.status_code == 404


# ---- CORS -------------------------------------------------------------------


def test_cors_allows_only_configured_origin(client):
    ok = client.options(
        "/api/strategies",
        headers={"Origin": "http://localhost:4200", "Access-Control-Request-Method": "GET"},
    )
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:4200"
    bad = client.options(
        "/api/strategies",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in bad.headers


def test_cors_lets_the_dev_origin_send_cookies_and_the_csrf_header(client):
    ok = client.options(
        "/api/halts/kill",
        headers={
            "Origin": "http://localhost:4200",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-csrf-token, content-type",
        },
    )
    assert ok.status_code == 200
    assert ok.headers.get("access-control-allow-credentials") == "true"
    assert "x-csrf-token" in ok.headers.get("access-control-allow-headers", "").lower()
    bad = client.options(
        "/api/halts/kill",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-csrf-token",
        },
    )
    assert "access-control-allow-origin" not in bad.headers


# ---- OpenAPI ----------------------------------------------------------------


def test_every_api_route_has_tags_and_response_model(app):
    ops = list(_operations(app))
    assert len(ops) >= 20
    for method, path, op in ops:
        assert path.startswith("/api/"), path
        assert op.get("tags"), (method, path)
        assert op.get("operationId"), (method, path)
        code, ok = next((k, v) for k, v in op["responses"].items() if k.startswith("2"))
        content = ok.get("content", {})
        media = next(iter(content.values()), {}) if content else {}
        if code != "204":  # No Content: nothing to describe
            assert media.get("schema") or media.get("itemSchema"), (method, path)
        for code, resp in op["responses"].items():
            if int(code) >= 400:
                assert list(resp["content"]) == ["application/problem+json"], (method, path)
        if path not in ("/api/health", "/api/health/live", "/api/health/ready"):
            assert "422" in op["responses"] or "404" in op["responses"], (method, path)


def test_openapi_declares_bearer_security(client):
    spec = client.get("/openapi.json").json()
    schemes = spec["components"]["securitySchemes"]
    assert any(s.get("scheme", "").lower() == "bearer" for s in schemes.values())


# ---- static UI --------------------------------------------------------------


def test_serves_built_ui_with_spa_fallback(settings, seeded, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>")
    (dist / "assets" / "main.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("top secret")
    settings.api.ui_dist = dist
    settings.api.allowed_hosts = ["testserver"]
    app = create_app(settings)
    with TestClient(app, client=LOOPBACK) as c:
        assert c.get("/").text == "<html>app</html>"
        assert c.get("/assets/main.js").text == "console.log(1)"
        assert c.get("/strategies/abc").text == "<html>app</html>"
        # traversal never escapes dist
        for evil in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/..%2f..%2fsecret.txt"):
            assert "top secret" not in c.get(evil).text
        # API 404s stay API 404s
        assert c.get("/api/nope").status_code == 404


def _sse_events(client: TestClient, url: str, **kw) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    name = "message"
    with client.stream("GET", url, **kw) as resp:
        assert resp.status_code == 200, resp.read()
        for line in resp.iter_lines():
            if line.startswith("event:"):
                name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                events.append((name, json.loads(line[len("data:") :])))
    return events


def test_job_events_stream_ends_for_a_job_no_runner_tracks(client, app):
    # A row left non-terminal (e.g. its final status write kept failing)
    # must not keep the stream polling forever.
    orphan = app.state.services.runner.store.create("backtest", {})
    events = _sse_events(client, f"/api/jobs/{orphan.id}/events")
    name, data = events[-1]
    assert name == "end"
    assert data["reason"] == "untracked"
    assert data["status"] == "queued"


def test_job_events_stream_times_out(settings, seeded, fake_source):
    import threading

    settings.api.allowed_hosts = ["testserver"]
    settings.api.sse_max_stream_seconds = 0.3
    app = create_app(settings, source_factory=lambda: fake_source, sse_poll_seconds=0.02)
    gate = threading.Event()
    with TestClient(app, client=LOOPBACK) as c:
        runner = app.state.services.runner
        runner.register("block", lambda p, ctx: gate.wait(10))
        job = runner.submit("block", {})
        try:
            started = time.monotonic()
            events = _sse_events(c, f"/api/jobs/{job.id}/events")
            assert time.monotonic() - started < 5
        finally:
            gate.set()
    name, data = events[-1]
    assert name == "end"
    assert data["reason"] == "timeout"


def test_unhandled_error_log_scrubs_configured_secrets(settings, seeded, capsys):
    settings.api.allowed_hosts = ["testserver"]
    settings.sources.eodhd.api_key = "vendor-key-xyz"
    app = create_app(settings)

    @app.get("/api/_boom")
    def boom() -> dict:
        raise RuntimeError("upstream said: bad key vendor-key-xyz")

    with TestClient(app, client=LOOPBACK, raise_server_exceptions=False) as c:
        resp = c.get("/api/_boom")
    assert resp.status_code == 500
    assert "vendor-key-xyz" not in resp.text
    out = capsys.readouterr().out
    assert "api.unhandled_error" in out
    assert "vendor-key-xyz" not in out
