"""The scheduled ``calendars_refresh`` job on every scheduler backend
(roadmap 20.7): the window around the fire's date, the calendars filled
once, and a failed calendar reported as a failed run."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from stonks.api import create_app
from stonks.calendars.store import CalendarStore
from stonks.scheduling.api_backend import (
    ApiExecutor,
    calendar_job_outcome,
    calendar_refresh_body,
)
from stonks.scheduling.api_client import SchedulerApiClient
from stonks.scheduling.in_process import InProcessExecutor
from stonks.scheduling.local import LocalExecutor
from stonks.store.lake import DuckDBLake
from tests.fixtures.calendars import FakeCalendarSource
from tests.integration.app.conftest import API_TOKEN
from tests.integration.app.test_scheduling_backends import BASE, _ctx, bridge

AS_OF = date(2026, 10, 1)


@pytest.fixture
def source() -> FakeCalendarSource:
    return FakeCalendarSource()


@pytest.fixture
def api_executor(settings, seeded, source):
    app = create_app(settings, source_factory=lambda: source, sse_poll_seconds=0.02)
    with TestClient(app, client=("127.0.0.1", 50000)) as tc:
        ex = ApiExecutor(
            SchedulerApiClient(BASE, token=API_TOKEN, transport=bridge(tc)), poll_seconds=0.02
        )
        yield ex
        ex.close()


@pytest.fixture
def in_process_executor(settings, seeded, source):
    from stonks.app.context import AppContext
    from stonks.app.services import Services

    svc = Services.create(AppContext(settings, source_factory=lambda: source))
    svc.start()
    yield InProcessExecutor(svc)
    svc.shutdown()


@pytest.fixture
def local_executor(settings, seeded, source, monkeypatch):
    from stonks.scheduling import local

    monkeypatch.setattr(local, "build_source", lambda source_id, sources: source)
    return LocalExecutor()


def test_body_spans_the_fire_date_and_passes_filters(settings):
    ctx, _ = _ctx(
        settings,
        None,
        "calendars_refresh",
        AS_OF,
        days_back=2,
        days_ahead=10,
        countries=["US"],
        alerts=False,
    )
    body = calendar_refresh_body(ctx)
    assert body["start"] == (AS_OF - timedelta(days=2)).isoformat()
    assert body["end"] == (AS_OF + timedelta(days=10)).isoformat()
    assert body["countries"] == ["US"]
    assert body["alerts"] is False
    assert "tickers" not in body


def test_outcome_fails_on_an_error_run_or_a_failed_job():
    ok = calendar_job_outcome("succeeded", None, {"status": "ok", "alerts": {"sent": 2}}, "j")
    assert ok.status == "succeeded" and ok.detail["alerts_sent"] == 2
    assert calendar_job_outcome("succeeded", None, {"status": "error"}, "j").status == "failed"
    failed = calendar_job_outcome("failed", "boom", None, "j")
    assert failed.status == "failed" and failed.detail["error"] == "boom"


@pytest.mark.parametrize("backend", ["api", "in_process", "local"])
def test_refresh_fills_the_calendars(settings, backend, request, source):
    ex = request.getfixturevalue(f"{backend}_executor")
    ctx, _ = _ctx(settings, ex, "calendars_refresh", AS_OF)
    out = ex.execute(ctx)
    assert out.status == "succeeded", out.detail
    assert out.detail["calendars_ok"] == 3
    assert {c[0] for c in source.calls} == {"earnings", "dividends", "economic"}
    assert {(c[1], c[2]) for c in source.calls} == {
        (AS_OF - timedelta(days=7), AS_OF + timedelta(days=35))
    }
    with DuckDBLake(settings.lake.path) as lake:
        store = CalendarStore(lake)
        assert [e.ticker for e in store.earnings(AS_OF, AS_OF + timedelta(days=35))] == ["AAPL.US"]


def test_a_down_calendar_is_a_partial_run_not_a_crash(settings, local_executor, source):
    source.fail = {"economic"}
    ctx, _ = _ctx(settings, local_executor, "calendars_refresh", AS_OF)
    out = local_executor.execute(ctx)
    assert out.detail["calendars_failed"] == 1
    assert out.detail["calendars_ok"] == 2
