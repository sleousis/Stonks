"""The ``api`` backend and its client over a mock transport (no network)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import httpx2
import pytest

from stonks.config import Settings
from stonks.notify import Notification, Notifier
from stonks.scheduling.api_backend import ApiExecutor, JobWaitTimeoutError, tick_job_outcome
from stonks.scheduling.api_client import (
    SchedulerApiClient,
    SchedulerApiError,
    SchedulerApiUnavailableError,
)
from stonks.scheduling.backends import BackendConfigError, build_executor
from stonks.scheduling.config import SchedulerConfig, resolve_backend
from stonks.scheduling.jobs import JobSpec, RunContext
from stonks.scheduling.local import LocalExecutor
from stonks.scheduling.triggers import Fire, SessionTrigger

TOKEN = "tok-secret-123"


class Recorder(Notifier):
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def _send(self, n: Notification) -> None:
        self.sent.append(n)


class FakeApi:
    """Routes the scheduler uses, with scripted job progress."""

    def __init__(self, job_statuses=("queued", "running", "succeeded"), error=None, result=None):
        self.requests: list[tuple[str, str, dict | None]] = []
        self._statuses = list(job_statuses)
        self._error = error
        self._result = result or {}
        self.health = {"healthy": True, "checked_at": "2026-09-25T21:00:00+00:00", "checks": []}

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.requests.append((request.method, path, body))
        assert request.headers.get("authorization") == f"Bearer {TOKEN}"
        if request.method == "POST" and path in ("/api/ticks", "/api/ingest/runs"):
            return httpx2.Response(202, json={"id": "job_1", "status": "queued"})
        if path == "/api/jobs/job_1":
            status = self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]
            return httpx2.Response(
                200, json={"id": "job_1", "status": status, "error": self._error}
            )
        if path.endswith("/jobs/job_1/result"):
            return httpx2.Response(200, json=self._result)
        if request.method == "POST" and path == "/api/health/run":
            return httpx2.Response(200, json=self.health)
        return httpx2.Response(404, json={"title": "Not Found", "detail": path})


def _executor(api: FakeApi, **kw) -> tuple[ApiExecutor, list[float]]:
    sleeps: list[float] = []
    client = SchedulerApiClient(
        "http://127.0.0.1:8000", token=TOKEN, transport=httpx2.MockTransport(api.handler)
    )
    return ApiExecutor(client, poll_seconds=1.5, sleep=sleeps.append, **kw), sleeps


def _ctx(ex, action: str, as_of: date, **params) -> tuple[RunContext, Recorder]:
    notifier = Recorder()
    at = datetime(as_of.year, as_of.month, as_of.day, 21, tzinfo=UTC)
    ctx = RunContext(
        spec=JobSpec(action, action, SessionTrigger("XNYS"), params=params),
        fire=Fire(at, as_of, as_of.isoformat()),
        run_id="srun_x",
        now=at,
        settings=Settings(production={"universe": ["AAPL.US"]}),
        notifier=notifier,
        executor=ex,
    )
    return ctx, notifier


FRIDAY = date(2026, 9, 25)


# ---- client ---------------------------------------------------------------------------


def test_client_refuses_token_over_plain_http_to_remote_host():
    with pytest.raises(ValueError, match="plain http"):
        SchedulerApiClient("http://api:8000", token=TOKEN)
    SchedulerApiClient("http://api:8000", token=TOKEN, trusted_hosts=["api"]).close()
    SchedulerApiClient("https://stonks.example", token=TOKEN).close()
    SchedulerApiClient("http://localhost:8000", token=TOKEN).close()


def test_client_errors_are_typed_and_redacted():
    def handler(request):
        return httpx2.Response(401, json={"title": "Unauthorized", "detail": f"bad {TOKEN}"})

    client = SchedulerApiClient(
        "http://127.0.0.1:8000", token=TOKEN, transport=httpx2.MockTransport(handler)
    )
    with pytest.raises(SchedulerApiError) as err:
        client.get("/api/jobs/x")
    assert err.value.status == 401 and TOKEN not in str(err.value)

    def down(request):
        raise httpx2.ConnectError("refused")

    client = SchedulerApiClient(
        "http://127.0.0.1:8000", token=TOKEN, transport=httpx2.MockTransport(down)
    )
    with pytest.raises(SchedulerApiUnavailableError):
        client.get("/api/jobs/x")
    assert TOKEN not in repr(client)


def test_post_needs_a_token():
    client = SchedulerApiClient("http://127.0.0.1:8000", token=None)
    with pytest.raises(SchedulerApiError, match="STONKS_API_TOKEN"):
        client.post("/api/ticks", {})


# ---- backend selection ------------------------------------------------------------------


def test_backend_resolution():
    cfg = SchedulerConfig()
    assert resolve_backend(cfg, {}) == "local"
    assert resolve_backend(cfg, {"STONKS_API_URL": "http://127.0.0.1:8000"}) == "api"
    assert resolve_backend(SchedulerConfig(api_url="http://127.0.0.1:8000"), {}) == "api"
    assert resolve_backend(SchedulerConfig(backend="local", api_url="http://x"), {}) == "local"
    assert isinstance(build_executor(cfg, {}), LocalExecutor)
    ex = build_executor(cfg, {"STONKS_API_URL": "http://127.0.0.1:8000", "STONKS_API_TOKEN": TOKEN})
    assert isinstance(ex, ApiExecutor)
    ex.close()
    with pytest.raises(BackendConfigError, match="inside `stonks serve`"):
        build_executor(SchedulerConfig(backend="in_process"), {})
    with pytest.raises(BackendConfigError, match="STONKS_API_URL"):
        build_executor(SchedulerConfig(backend="api"), {})


def test_trusted_hosts_can_come_from_the_environment():
    env = {"STONKS_API_URL": "http://api:8000", "STONKS_API_TOKEN": TOKEN}
    with pytest.raises(ValueError, match="plain http"):
        build_executor(SchedulerConfig(), env)
    ex = build_executor(SchedulerConfig(), {**env, "STONKS_API_TRUSTED_HOSTS": "api, other"})
    assert isinstance(ex, ApiExecutor)
    ex.close()


def test_compose_runs_the_scheduler_loop_against_the_api():
    from pathlib import Path

    import yaml

    compose = yaml.safe_load(
        (Path(__file__).parents[2] / "deploy" / "compose.yaml").read_text(encoding="utf-8")
    )
    scheduler = compose["services"]["scheduler"]
    assert scheduler["command"][-1] == "run"
    env = scheduler["environment"]
    assert env["STONKS_API_URL"] == "http://api:8000"
    assert env["STONKS_API_TRUSTED_HOSTS"] == "api"
    assert env["STONKS_DATA_DIR"] == "/data"


def _seconds(value: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600}
    return int(value[:-1]) * units[value[-1]]


def test_compose_lets_a_running_tick_finish_before_the_api_is_killed():
    """A tick is never interrupted mid-way (``JobRunner.shutdown``), so the
    api container must wait at least as long as the scheduler waits for it,
    not Docker's default 10 s."""
    from pathlib import Path

    import yaml

    compose = yaml.safe_load(
        (Path(__file__).parents[2] / "deploy" / "compose.yaml").read_text(encoding="utf-8")
    )
    api = _seconds(compose["services"]["api"]["stop_grace_period"])
    scheduler = _seconds(compose["services"]["scheduler"]["stop_grace_period"])
    assert api >= 5 * 60 and api >= scheduler


# ---- actions ----------------------------------------------------------------------------


def test_tick_posts_the_fire_date_and_waits_for_the_job():
    api = FakeApi(
        result={"tick_id": "tick_2026-09-25_x", "status": "ok", "orders_placed": 2, "fills": 2}
    )
    ex, sleeps = _executor(api)
    ctx, notifier = _ctx(ex, "tick", FRIDAY)
    out = ex.execute(ctx)
    assert out.status == "succeeded" and out.detail["tick_id"] == "tick_2026-09-25_x"
    method, path, body = api.requests[0]
    assert (method, path) == ("POST", "/api/ticks")
    # the configured universe: a full tick, not a scoped one (TO-04)
    assert body == {
        "as_of": "2026-09-25",
        "tickers": ["AAPL.US"],
        "scoped": False,
        # buys need the bar of the session that closed by the fire (TO-10)
        "bars_due_at": "2026-09-25T21:00:00+00:00",
        "dry_run": False,
    }
    assert sleeps == [1.5, 1.5]  # queued, running, then succeeded
    assert api.requests[-1][1] == "/api/ticks/jobs/job_1/result"


def test_a_tick_job_with_its_own_tickers_is_scoped():
    """TO-04: a job's ``params.tickers`` (the crypto tick) must leave other
    holdings alone."""
    api = FakeApi(result={"tick_id": "t", "status": "ok", "orders_placed": 0, "fills": 0})
    ex, _ = _executor(api)
    ctx, _ = _ctx(ex, "tick", FRIDAY, tickers=["BTC-USD.CC"])
    ex.execute(ctx)
    body = api.requests[0][2]
    assert body["tickers"] == ["BTC-USD.CC"] and body["scoped"] is True


def test_ingest_posts_the_lookback_window():
    api = FakeApi(
        job_statuses=("succeeded",),
        result={"run_id": 7, "status": "ok", "tickers_ok": 1, "tickers_failed": 0},
    )
    ex, _ = _executor(api)
    ctx, _ = _ctx(ex, "ingest_prices", FRIDAY, lookback_days=3, source="yahoo")
    out = ex.execute(ctx)
    assert out.status == "succeeded" and out.detail["ingest_run_id"] == 7
    assert api.requests[0][2] == {
        "kind": "prices",
        "source": "yahoo",
        "tickers": ["AAPL.US"],
        "since": "2026-09-22",
        "until": "2026-09-25",
    }


def test_ingest_metadata_posts_a_metadata_run():
    api = FakeApi(
        job_statuses=("succeeded",),
        result={"run_id": 8, "status": "ok", "tickers_ok": 1, "tickers_failed": 0},
    )
    ex, _ = _executor(api)
    ctx, _ = _ctx(ex, "ingest_metadata", FRIDAY, source="yahoo")
    out = ex.execute(ctx)
    assert out.status == "succeeded" and out.detail["ingest_run_id"] == 8
    assert api.requests[0][:2] == ("POST", "/api/ingest/runs")
    assert api.requests[0][2] == {"kind": "metadata", "source": "yahoo", "tickers": ["AAPL.US"]}


def test_ingest_borrow_posts_a_borrow_run_when_a_gateway_is_set():
    api = FakeApi(
        job_statuses=("succeeded",),
        result={"run_id": 9, "status": "ok", "tickers_ok": 1, "tickers_failed": 0},
    )
    ex, _ = _executor(api)
    ctx, _ = _ctx(ex, "ingest_borrow", FRIDAY, markets=["usa"])
    gateways = {"paper": {"host": "127.0.0.1", "port": 4002, "mode": "paper"}}
    settings = Settings(brokers={"ibkr": {"gateways": gateways}})
    ctx = RunContext(**{**ctx.__dict__, "settings": settings})
    out = ex.execute(ctx)
    assert out.status == "succeeded" and out.detail["ingest_run_id"] == 9
    assert api.requests[0][:2] == ("POST", "/api/ingest/runs")
    assert api.requests[0][2] == {"kind": "borrow", "markets": ["usa"]}


def test_closed_day_makes_no_request():
    api = FakeApi()
    ex, _ = _executor(api)
    ctx, _ = _ctx(ex, "tick", date(2026, 11, 26))  # Thanksgiving
    assert ex.execute(ctx).detail["reason"] == "market_closed"
    assert api.requests == []


def test_failed_tick_job_was_alerted_by_the_tick():
    api = FakeApi(job_statuses=("failed",), error="RuntimeError: broker down")
    ex, _ = _executor(api)
    out = ex.execute(_ctx(ex, "tick", FRIDAY)[0])
    assert out.status == "failed" and out.alerted


def test_tick_outcome_classification():
    backdated = "ConflictError: refusing to trade as_of 2026-09-24: the portfolio already has..."
    assert tick_job_outcome("failed", backdated, None, "j").status == "skipped"
    rejected = tick_job_outcome("failed", "ValidationError: universe is empty", None, "j")
    assert rejected.status == "failed" and not rejected.alerted


def test_health_unhealthy_alerts_here():
    api = FakeApi()
    api.health = {
        "healthy": False,
        "checked_at": "2026-09-25T21:00:00+00:00",
        "checks": [{"name": "freshness:AAPL.US", "ok": False, "detail": "stale"}],
    }
    ex, _ = _executor(api)
    ctx, notifier = _ctx(ex, "health", FRIDAY)
    out = ex.execute(ctx)
    assert out.status == "failed" and out.alerted
    assert out.detail["failed_checks"] == ["freshness:AAPL.US"]
    assert len(notifier.sent) == 1
    # the scheduled job syncs the operational halt: the POST route, never the GET
    assert api.requests[0][:2] == ("POST", "/api/health/run")


def test_wait_times_out():
    api = FakeApi(job_statuses=("running",))
    clock = iter(range(0, 10_000, 100))
    ex, _ = _executor(api, timeout_seconds=250, monotonic=lambda: next(clock))
    with pytest.raises(JobWaitTimeoutError):
        ex.execute(_ctx(ex, "tick", FRIDAY)[0])


def test_stop_abandons_the_wait():
    import threading

    api = FakeApi(job_statuses=("running",))
    ex, _ = _executor(api)
    stop = threading.Event()
    stop.set()
    ex.bind_stop(stop)
    with pytest.raises(JobWaitTimeoutError, match="keeps running"):
        ex.execute(_ctx(ex, "tick", FRIDAY)[0])
