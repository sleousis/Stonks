"""Scheduler backends against the real app: the ``api`` backend through the
FastAPI app (TestClient behind an httpx2 MockTransport) and the
``in_process`` backend on the app's own services and JobRunner."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx2
import pytest
from fastapi.testclient import TestClient

from stonks.api import create_app
from stonks.notify import Notification, Notifier
from stonks.scheduling.api_backend import ApiExecutor
from stonks.scheduling.api_client import SchedulerApiClient
from stonks.scheduling.config import SchedulerConfig
from stonks.scheduling.in_process import InProcessExecutor, start_in_process_scheduler
from stonks.scheduling.jobs import JobSpec, RunContext
from stonks.scheduling.runs import RunStore
from stonks.scheduling.scheduler import InstanceLock, Scheduler
from stonks.scheduling.triggers import Fire, SessionTrigger
from stonks.store.state import SqliteState
from tests.integration.app.conftest import API_TOKEN

BASE = "http://127.0.0.1:8000"
_HOP = {"content-length", "content-encoding", "transfer-encoding"}


class Recorder(Notifier):
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def _send(self, n: Notification) -> None:
        self.sent.append(n)


def bridge(tc: TestClient) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
        resp = tc.request(
            request.method, request.url.raw_path.decode(), headers=headers, content=request.content
        )
        out = [(k, v) for k, v in resp.headers.items() if k.lower() not in _HOP | {"host"}]
        return httpx2.Response(resp.status_code, headers=out, content=resp.content)

    return httpx2.MockTransport(handler)


@pytest.fixture
def api_executor(settings, seeded, fake_source):
    app = create_app(settings, source_factory=lambda: fake_source, sse_poll_seconds=0.02)
    with TestClient(app, client=("127.0.0.1", 50000)) as tc:
        client = SchedulerApiClient(BASE, token=API_TOKEN, transport=bridge(tc))
        ex = ApiExecutor(client, poll_seconds=0.02)
        yield ex
        ex.close()


def _ctx(settings, ex, action: str, as_of: date, **params) -> tuple[RunContext, Recorder]:
    notifier = Recorder()
    at = datetime(as_of.year, as_of.month, as_of.day, 21, tzinfo=UTC)
    return (
        RunContext(
            spec=JobSpec(action, action, SessionTrigger("XNYS"), params=params),
            fire=Fire(at, as_of, as_of.isoformat()),
            run_id="srun_it",
            now=at,
            settings=settings,
            notifier=notifier,
            executor=ex,
        ),
        notifier,
    )


def _ticks(settings) -> list:
    with SqliteState(settings.state.path) as s:
        return [r["id"] for r in s.sql("SELECT id FROM tick_runs ORDER BY started_at")]


# ---- api backend -------------------------------------------------------------------------


def test_api_tick_runs_through_the_api(settings, api_executor):
    ctx, _ = _ctx(settings, api_executor, "tick", date(2026, 3, 23), tickers=["UP.US"])
    out = api_executor.execute(ctx)
    assert out.status == "succeeded", out.detail
    assert out.detail["tick_id"].startswith("tick_2026-03-23_")
    assert out.detail["tick_id"] in _ticks(settings)


def test_api_backdated_tick_is_skipped(settings, api_executor):
    ctx, _ = _ctx(settings, api_executor, "tick", date(2026, 3, 19), tickers=["UP.US"])
    out = api_executor.execute(ctx)
    assert out.status == "skipped" and out.detail["reason"] == "backdated"


def test_api_ingest_runs_through_the_api(settings, api_executor):
    ctx, _ = _ctx(settings, api_executor, "ingest_prices", date(2026, 4, 2), tickers=["NEW.US"])
    out = api_executor.execute(ctx)
    assert out.status == "succeeded", out.detail
    assert out.detail["tickers_ok"] == 1


def test_api_health_reads_the_report(settings, api_executor):
    ctx, notifier = _ctx(settings, api_executor, "health", date(2026, 9, 25), tickers=["UP.US"])
    out = api_executor.execute(ctx)
    # seeded bars end 2026-04-01: stale by September
    assert out.status == "failed" and "freshness:UP.US" in out.detail["failed_checks"]
    assert len(notifier.sent) == 1
    # the scheduled job (not a report read) opens the operational halt
    with SqliteState(settings.state.path) as s:
        kinds = [r["kind"] for r in s.sql("SELECT kind FROM risk_halts WHERE cleared_at IS NULL")]
    assert kinds == ["operational"]


def test_scheduler_loop_on_the_api_backend(settings, api_executor, tmp_path):
    store = RunStore(settings.state.path)
    store.migrate()
    spec = JobSpec("tick", "tick", SessionTrigger("XNYS"), params={"tickers": ["UP.US"]})
    sched = Scheduler([spec], store, settings=settings, notifier=Recorder(), executor=api_executor)
    fire = spec.trigger.next_fire(datetime(2026, 3, 23, tzinfo=UTC))
    result = sched.run_one(spec, fire)
    assert result.status == "succeeded"
    assert store.get("tick", "2026-03-23").detail["tick_id"].startswith("tick_2026-03-23_")


# ---- in_process backend ------------------------------------------------------------------


def test_in_process_tick_and_ingest(settings, services):
    ex = InProcessExecutor(services)
    ctx, _ = _ctx(settings, ex, "tick", date(2026, 3, 23), tickers=["UP.US"])
    out = ex.execute(ctx)
    assert out.status == "succeeded", out.detail
    ctx, _ = _ctx(settings, ex, "ingest_prices", date(2026, 4, 2), tickers=["NEW.US"])
    assert ex.execute(ctx).status == "succeeded"
    # asset classes come from the lake here: UP.US is an equity, closed on Thanksgiving
    ctx, _ = _ctx(settings, ex, "tick", date(2026, 11, 26), tickers=["UP.US"])
    assert ex.execute(ctx).detail["reason"] == "market_closed"


@pytest.mark.parametrize(("as_of", "buys"), [(date(2026, 4, 2), 0), (date(2026, 4, 1), 1)])
def test_a_scheduled_tick_after_a_failed_ingest_places_no_buys(settings, services, as_of, buys):
    """TO-10: the seeded bars end 2026-04-01. On 04-02 the session's bar is
    missing (the price ingest failed), so the scheduled tick buys nothing,
    though the 7-day staleness window alone would let it."""
    from stonks.core.protocols import SurvivalReport
    from stonks.registry.store import StrategyRegistry
    from stonks.strategies.examples.buy_and_hold import BuyAndHold
    from tests.fixtures.governance import seed_status

    with SqliteState(settings.state.path) as s:
        registry = StrategyRegistry(state=s, artifacts_dir=settings.registry.artifacts_dir)
        sid = registry.register(
            BuyAndHold({"ticker": "FLAT.US", "allocation": 0.2}),
            reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
            strategy_id="bah_flat",
        )
        seed_status(registry, "bah_active", "retired")
        seed_status(registry, sid, "active")
    ex = InProcessExecutor(services)
    ctx, _ = _ctx(settings, ex, "tick", as_of, tickers=["FLAT.US"])
    out = ex.execute(ctx)
    assert out.status == "succeeded", out.detail
    assert out.detail["orders_placed"] == buys


def test_in_process_health(settings, services):
    ex = InProcessExecutor(services)
    ctx, notifier = _ctx(settings, ex, "health", date(2026, 9, 25), tickers=["UP.US"])
    assert ex.execute(ctx).status == "failed"
    assert len(notifier.sent) == 1


def test_start_and_stop_inside_the_api_process(settings, services, tmp_path):
    config = SchedulerConfig(lock_path=tmp_path / "s.lock", poll_seconds=3600)
    handle = start_in_process_scheduler(services, settings, config=config, notifier=Recorder())
    assert handle is not None and handle.thread.is_alive()
    # a second scheduler (e.g. a separate worker) can't start meanwhile
    assert start_in_process_scheduler(services, settings, config=config) is None
    handle.stop(timeout=10)
    assert not handle.thread.is_alive()
    InstanceLock(tmp_path / "s.lock").acquire()  # released


def test_disabled_in_process_scheduler_does_not_start(settings, services):
    assert (
        start_in_process_scheduler(services, settings, config=SchedulerConfig(enabled=False))
        is None
    )
