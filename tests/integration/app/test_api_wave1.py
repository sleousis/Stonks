"""REST API routes for the Wave 1 features: sources, risk, shadow, health
report, P&L, brokers, cost models, typed job results and stream tokens."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from stonks.api import create_app
from tests.integration.app.test_api import AUTH, LOOPBACK, REMOTE


@pytest.fixture
def app(settings, seeded, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    return create_app(settings, source_factory=lambda: fake_source, sse_poll_seconds=0.02)


@pytest.fixture
def client(app):
    with TestClient(app, client=LOOPBACK) as c:
        yield c


@pytest.fixture
def remote(app):
    with TestClient(app, client=REMOTE) as c:
        yield c


# ---- sources ----------------------------------------------------------------


def test_list_data_sources(client):
    resp = client.get("/api/sources")
    assert resp.status_code == 200
    by_id = {s["id"]: s for s in resp.json()}
    assert set(by_id) == {"eodhd", "yahoo", "defillama"}
    assert by_id["eodhd"]["default"] is True
    assert by_id["eodhd"]["configured"] is False  # no key in the test settings


def test_ingest_request_accepts_source(client):
    resp = client.post(
        "/api/ingest/runs",
        json={"kind": "prices", "tickers": ["NEW.US"], "source": "yahoo"},
        headers=AUTH,
    )
    assert resp.status_code == 202
    bad = client.post(
        "/api/ingest/runs",
        json={"kind": "prices", "tickers": ["NEW.US"], "source": "bogus"},
        headers=AUTH,
    )
    assert bad.status_code == 422


# ---- typed job results --------------------------------------------------------

BAH = "stonks.strategies.examples.buy_and_hold:BuyAndHold"


def _backtest_body() -> dict:
    return {
        "strategy": {"class_path": BAH, "params": {"ticker": "UP.US", "allocation": 1.0}},
        "universe": ["UP.US"],
        "start": "2025-10-01",
        "end": "2026-04-01",
    }


def _done(client: TestClient, job_id: str) -> dict:
    from tests.integration.app.test_api import _wait_job

    return _wait_job(client, job_id)


def test_typed_backtest_result_route(client):
    job = client.post("/api/lab/backtests", json=_backtest_body(), headers=AUTH).json()
    assert _done(client, job["id"])["status"] == "succeeded"
    resp = client.get(f"/api/lab/backtests/{job['id']}/result")
    assert resp.status_code == 200
    body = resp.json()
    assert body["final_return"] > 0.5
    assert body["equity"][0]["value"] == pytest.approx(10_000.0)
    # wrong kind is not found
    assert client.get(f"/api/lab/runs/{job['id']}/result").status_code == 404
    assert client.get("/api/lab/backtests/job_missing/result").status_code == 404


def test_typed_result_of_unfinished_job_is_conflict(client, app):
    orphan = app.state.services.runner.store.create("backtest", {})
    resp = client.get(f"/api/lab/backtests/{orphan.id}/result")
    assert resp.status_code == 409
    assert "queued" in resp.json()["detail"]


def test_typed_ingest_and_tick_result_routes(client):
    ingest = client.post(
        "/api/ingest/runs", json={"kind": "prices", "tickers": ["NEW.US"]}, headers=AUTH
    ).json()
    assert _done(client, ingest["id"])["status"] == "succeeded"
    body = client.get(f"/api/ingest/jobs/{ingest['id']}/result").json()
    assert body["tickers_ok"] == 1

    tick = client.post(
        "/api/ticks",
        json={"dry_run": True, "tickers": ["UP.US"], "as_of": "2026-03-27"},
        headers=AUTH,
    ).json()
    assert _done(client, tick["id"])["status"] == "succeeded"
    body = client.get(f"/api/ticks/jobs/{tick['id']}/result").json()
    assert body["dry_run"] is True


def test_tick_runs_have_a_typed_summary(client, seeded):
    page = client.get("/api/ticks").json()
    summary = page["items"][0]["summary"]
    assert summary["orders_placed"] >= 0
    assert isinstance(summary["risk_adjustments"], list)
    assert {s["strategy_id"] for s in summary["shadow"]} == {"bah_shadow"}
    detail = client.get(f"/api/ticks/{seeded['tick_id']}").json()
    assert detail["summary"]["winner_strategy_id"] == seeded["active_id"]


def test_job_result_and_tick_summary_schemas_are_in_openapi(app):
    schemas = app.openapi()["components"]["schemas"]
    for name in (
        "BacktestResult",
        "LabRunView",
        "TickResultView",
        "IngestResultView",
        "TickSummary",
        "RiskAdjustmentView",
        "ShadowOutcomeView",
    ):
        assert name in schemas, name
    tick_run = schemas["TickRun"]["properties"]["summary"]
    assert "TickSummary" in str(tick_run)


# ---- stream tokens (EventSource can't send a bearer header) -------------------


@pytest.fixture
def closed_client(settings, seeded, fake_source):
    """Reads need auth even on loopback."""
    settings.api.allowed_hosts = ["testserver"]
    settings.api.open_reads_on_loopback = False
    app = create_app(settings, source_factory=lambda: fake_source, sse_poll_seconds=0.02)
    with TestClient(app, client=LOOPBACK) as c:
        yield c


def _stream_status(client: TestClient, url: str) -> tuple[int, list[str]]:
    lines: list[str] = []
    with client.stream("GET", url) as resp:
        if resp.status_code == 200:
            lines = [ln for ln in resp.iter_lines() if ln.startswith("event:")]
        else:
            resp.read()
        return resp.status_code, lines


def test_stream_token_lets_a_browser_stream_when_reads_are_closed(closed_client):
    job = closed_client.post("/api/lab/backtests", json=_backtest_body(), headers=AUTH).json()
    assert _stream_status(closed_client, f"/api/jobs/{job['id']}/events")[0] == 401

    assert closed_client.post(f"/api/jobs/{job['id']}/stream-token").status_code == 401
    resp = closed_client.post(f"/api/jobs/{job['id']}/stream-token", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["expires_at"]
    assert body["events_url"] == f"/api/jobs/{job['id']}/events?token={body['token']}"

    status, events = _stream_status(closed_client, body["events_url"])
    assert status == 200
    assert events[-1] == "event: done"


def test_stream_token_is_scoped_to_its_job(closed_client):
    a = closed_client.post("/api/lab/backtests", json=_backtest_body(), headers=AUTH).json()
    b = closed_client.post("/api/lab/backtests", json=_backtest_body(), headers=AUTH).json()
    token = closed_client.post(f"/api/jobs/{a['id']}/stream-token", headers=AUTH).json()["token"]
    assert _stream_status(closed_client, f"/api/jobs/{b['id']}/events?token={token}")[0] == 401
    assert _stream_status(closed_client, f"/api/jobs/{a['id']}/events?token=nope")[0] == 401
    # a stream token is not a bearer token for anything else
    other = closed_client.get("/api/strategies", headers={"Authorization": f"Bearer {token}"})
    assert other.status_code == 401
    assert closed_client.get(f"/api/jobs/{a['id']}?token={token}").status_code == 401


def test_stream_token_works_from_a_remote_peer(remote, client):
    job = client.post("/api/lab/backtests", json=_backtest_body(), headers=AUTH).json()
    token = remote.post(f"/api/jobs/{job['id']}/stream-token", headers=AUTH).json()["token"]
    assert _stream_status(remote, f"/api/jobs/{job['id']}/events")[0] == 401
    assert _stream_status(remote, f"/api/jobs/{job['id']}/events?token={token}")[0] == 200


def test_stream_token_dies_with_its_user(closed_client, settings):
    """The token names the user who asked for it: once that person is
    disabled (or signed out of everything) it stops opening the stream."""
    from stonks.accounts import DEFAULT_OWNER_ID
    from stonks.store.state import SqliteState

    job = closed_client.post("/api/lab/backtests", json=_backtest_body(), headers=AUTH).json()
    url = closed_client.post(f"/api/jobs/{job['id']}/stream-token", headers=AUTH).json()[
        "events_url"
    ]
    with SqliteState(settings.state.path) as state:
        state.execute("UPDATE users SET status = 'disabled' WHERE id = ?", [DEFAULT_OWNER_ID])
    assert _stream_status(closed_client, url)[0] == 401


def test_stream_token_for_unknown_job_is_404(client):
    assert client.post("/api/jobs/job_missing/stream-token", headers=AUTH).status_code == 404


# ---- risk policy --------------------------------------------------------------


def test_get_risk_policy(settings, seeded, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    settings.production.risk = settings.production.risk.model_copy(
        update={"max_open_positions": 5, "max_weight_per_asset_class": {"crypto": 0.2}}
    )
    app = create_app(settings, source_factory=lambda: fake_source)
    with TestClient(app, client=LOOPBACK) as c:
        body = c.get("/api/risk/policy").json()
    assert body["enabled"] is True
    assert body["max_open_positions"] == 5
    assert body["max_weight_per_asset_class"] == {"crypto": 0.2}
    assert body["max_weight_per_ticker"] == 1.0


# ---- P&L ----------------------------------------------------------------------


def test_real_pnl_series(client):
    body = client.get("/api/pnl", headers=AUTH).json()
    assert body["strategy_id"] is None
    rows = body["rows"]
    assert len(rows) == 1  # the seeded tick wrote one snapshot
    assert rows[0]["total_value"] > 0
    assert rows[0]["daily_return"] is None
    assert rows[0]["days_elapsed"] is None  # first row: nothing before it
    assert rows[0]["drawdown"] == 0.0
    assert client.get("/api/pnl", params={"since": "2999-01-01"}, headers=AUTH).json()["rows"] == []


# ---- shadow -------------------------------------------------------------------


def _insert_shadow_decisions(settings, seeded) -> None:
    from stonks.store.state import SqliteState

    with SqliteState(settings.state.path) as s:
        for i, ticker in enumerate(["DOWN.US", "UP.US"]):
            s.execute(
                "INSERT INTO shadow_decisions (tick_id, strategy_id, as_of, ticker, side, "
                "quantity, price, status, created_at) VALUES (?, ?, ?, ?, 'buy', ?, ?, ?, ?)",
                [
                    seeded["tick_id"],
                    seeded["shadow_id"],
                    f"2026-03-2{i}",
                    ticker,
                    10.0 + i,
                    50.0,
                    "filled" if i == 0 else "rejected",
                    "2026-03-20T00:00:00+00:00",
                ],
            )


def test_list_shadow_decisions_filters_and_paginates(client, settings, seeded):
    _insert_shadow_decisions(settings, seeded)
    page = client.get("/api/shadow/decisions").json()
    assert page["total"] == 2
    assert page["items"][0]["as_of"] == "2026-03-21"  # newest first
    only = client.get("/api/shadow/decisions", params={"ticker": "DOWN.US"}).json()
    assert [d["status"] for d in only["items"]] == ["filled"]
    paged = client.get("/api/shadow/decisions", params={"limit": 1, "offset": 1}).json()
    assert len(paged["items"]) == 1 and paged["total"] == 2
    none = client.get("/api/shadow/decisions", params={"strategy_id": "bah_active"}).json()
    assert none["total"] == 0


def test_shadow_pnl_per_strategy(client, seeded):
    page = client.get("/api/shadow/pnl").json()
    assert page["total"] == 1
    row = page["items"][0]
    assert row["strategy_id"] == seeded["shadow_id"]
    assert row["status"] == "shadow"
    assert row["days"] == 1
    assert row["total_value"] == pytest.approx(10_000.0)

    series = client.get(f"/api/shadow/strategies/{seeded['shadow_id']}/pnl").json()
    assert series["strategy_id"] == seeded["shadow_id"]
    assert len(series["rows"]) == 1
    assert client.get("/api/shadow/strategies/nope/pnl").status_code == 404


# ---- health report -------------------------------------------------------------


def test_health_report_needs_auth_but_liveness_does_not(remote):
    assert remote.get("/api/health").status_code == 200
    assert remote.get("/api/health/report").status_code == 401
    assert remote.get("/api/health/report", headers=AUTH).status_code == 200


def test_health_report(client):
    body = client.get("/api/health/report", params={"tickers": ["UP.US", "NOPE.US"]}).json()
    checks = {c["name"]: c for c in body["checks"]}
    assert checks["freshness:NOPE.US"]["ok"] is False
    assert "freshness:UP.US" in checks
    assert checks["stuck_ticks"]["ok"] is True
    assert body["healthy"] is False
    assert body["checked_at"]
    # stale data opens the global operational halt, and the report lists it
    assert checks["risk_halts"]["ok"] is False
    assert "operational" in checks["risk_halts"]["detail"]


def test_health_report_exposes_its_thresholds(client, settings):
    """Roadmap 11.6: the report says which limits it judged against."""
    thresholds = client.get("/api/health/report").json()["thresholds"]
    assert thresholds == settings.production.health.model_dump()
    assert thresholds["max_bar_age_days"] == 4


# ---- brokers --------------------------------------------------------------------


def test_broker_info_defaults_to_simulated_without_keys(client):
    body = client.get("/api/brokers").json()
    assert body == {
        "kind": "simulated",
        "paper": True,
        "allow_live": False,
        "credentials_configured": False,
    }
    # the alpaca status route only applies when alpaca is the configured broker
    assert client.get("/api/brokers/alpaca/status").status_code == 409


def test_alpaca_status_without_keys_reports_not_connected(settings, seeded):
    settings.api.allowed_hosts = ["testserver"]
    settings.brokers.kind = "alpaca"
    with TestClient(create_app(settings), client=LOOPBACK) as c:
        body = c.get("/api/brokers/alpaca/status").json()
    assert body["connected"] is False
    assert "ALPACA_API_KEY" in body["error"]
    assert body["account"] is None


def test_alpaca_status_with_a_fake_connection(settings, seeded):
    from datetime import UTC, datetime

    from pydantic import SecretStr

    from stonks.app.context import AppContext
    from stonks.app.services import Services
    from stonks.execution.brokers.base import BrokerAccount, MarketClock

    class FakeAlpaca:
        def fetch_account(self):
            return BrokerAccount(
                cash=100.0, equity=150.0, buying_power=200.0, currency="USD", status="ACTIVE"
            )

        def get_market_clock(self):
            now = datetime(2026, 3, 20, 15, tzinfo=UTC)
            return MarketClock(timestamp=now, is_open=True, next_open=now, next_close=now)

    settings.api.allowed_hosts = ["testserver"]
    settings.brokers.kind = "alpaca"
    settings.brokers.alpaca.api_key = SecretStr("k-123")
    settings.brokers.alpaca.secret_key = SecretStr("s-456")
    svc = Services.create(AppContext(settings), broker_connector=lambda s: FakeAlpaca())
    with TestClient(create_app(settings, services=svc), client=LOOPBACK) as c:
        body = c.get("/api/brokers/alpaca/status").json()
        info = c.get("/api/brokers").json()
    assert body["connected"] is True
    assert body["account"]["equity"] == 150.0
    assert body["account"]["can_trade"] is True
    assert body["clock"]["is_open"] is True
    assert info["credentials_configured"] is True
    assert "k-123" not in str(info) and "s-456" not in str(body)


def test_alpaca_status_scrubs_errors(settings, seeded):
    from pydantic import SecretStr

    from stonks.app.context import AppContext
    from stonks.app.services import Services

    def boom(_settings):
        raise RuntimeError("401 for key k-123")

    settings.api.allowed_hosts = ["testserver"]
    settings.brokers.kind = "alpaca"
    settings.brokers.alpaca.api_key = SecretStr("k-123")
    settings.brokers.alpaca.secret_key = SecretStr("s-456")
    svc = Services.create(AppContext(settings), broker_connector=boom)
    with TestClient(create_app(settings, services=svc), client=LOOPBACK) as c:
        body = c.get("/api/brokers/alpaca/status").json()
    assert body["connected"] is False
    assert "k-123" not in body["error"]


# ---- cost models + lab options ---------------------------------------------------


def test_list_cost_models(client):
    by_name = {m["name"]: m for m in client.get("/api/lab/cost-models").json()}
    assert {"zero", "realistic"} <= set(by_name)
    realistic = by_name["realistic"]["settings"]
    assert realistic["asset_classes"]["crypto"]["fee_bps"] == 10.0
    assert by_name["zero"]["settings"]["impact_bps"] == 0.0


def test_backtest_with_cost_model_preset_costs_more(client):
    from tests.integration.app.test_api import _wait_job

    def final_return(extra: dict) -> float:
        job = client.post(
            "/api/lab/backtests", json={**_backtest_body(), **extra}, headers=AUTH
        ).json()
        assert _wait_job(client, job["id"])["status"] == "succeeded"
        return client.get(f"/api/lab/backtests/{job['id']}/result").json()["final_return"]

    assert final_return({"cost_model": "realistic"}) < final_return({"cost_model": "zero"})
    # BL-13: without a cost model the configured (realistic by default) costs apply
    assert final_return({}) == final_return({"cost_model": "realistic"})
    bad = client.post(
        "/api/lab/backtests",
        json={**_backtest_body(), "cost_model": "realistic", "slippage_bps": 5},
        headers=AUTH,
    )
    assert bad.status_code == 422


def _lab_body(**extra) -> dict:
    return {
        "strategy": {"class_path": "stonks.strategies.examples.momentum:Momentum"},
        "universe": ["UP.US", "DOWN.US"],
        "start": "2025-10-01",
        "end": "2026-04-01",
        "budget": 1,
        **extra,
    }


def test_lab_run_with_walk_forward_and_mcpt_options(client):
    from tests.integration.app.test_api import _wait_job

    body = _lab_body(
        survival_tests=["walk_forward", "permutation"],
        walk_forward={"n_splits": 2, "anchored": True},
        mcpt={"n_permutations": 2, "max_p_value": 0.5, "metric": "sharpe"},
    )
    job = client.post("/api/lab/runs", json=body, headers=AUTH).json()
    done = _wait_job(client, job["id"], timeout=120)
    assert done["status"] == "succeeded", done["error"]
    result = client.get(f"/api/lab/runs/{job['id']}/result").json()
    reports = {r["test_id"]: r for r in result["survival_reports"]}
    assert reports["walk_forward"]["metrics"]["n_folds"] == 2.0
    assert reports["mcpt"]["metrics"]["n_permutations"] == 2.0


def test_lab_run_options_require_their_test(client):
    resp = client.post(
        "/api/lab/runs",
        json=_lab_body(survival_tests=["oos"], walk_forward={"n_splits": 2}),
        headers=AUTH,
    )
    assert resp.status_code == 422
    resp = client.post(
        "/api/lab/runs",
        json=_lab_body(survival_tests=["permutation"], mcpt={"n_permutations": 0}),
        headers=AUTH,
    )
    assert resp.status_code == 422
