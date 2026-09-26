"""The scheduled ``universes_refresh`` job and universe-id ticks on every
scheduler backend (roadmap 10.5, integration step)."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from stonks.api import create_app
from stonks.scheduling.api_backend import ApiExecutor
from stonks.scheduling.api_client import SchedulerApiClient
from stonks.scheduling.in_process import InProcessExecutor
from stonks.scheduling.local import LocalExecutor
from stonks.store.lake import DuckDBLake
from stonks.universes import UniverseDefinition, UniverseStore
from tests.fixtures.universes import FakeListingSource, bars
from tests.integration.app.conftest import API_TOKEN
from tests.integration.app.test_scheduling_backends import BASE, _ctx, bridge

AS_OF = date(2026, 4, 1)


@pytest.fixture
def source() -> FakeListingSource:
    return FakeListingSource(prices={"NEW.US": bars("NEW.US", date(2026, 3, 2), AS_OF)})


def _store(settings, universe_id: str, tickers: list[str]) -> None:
    with DuckDBLake(settings.lake.path) as lake:
        UniverseStore(lake).save(
            UniverseDefinition(id=universe_id, kind="list", spec={"tickers": tickers})
        )


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


def test_skips_while_no_universe_is_stored(settings, api_executor):
    ctx, _ = _ctx(settings, api_executor, "universes_refresh", AS_OF)
    out = api_executor.execute(ctx)
    assert out.status == "skipped" and out.detail["reason"] == "no_universes"


@pytest.mark.parametrize("backend", ["api", "in_process"])
def test_refreshes_then_fills_recent_bars(settings, backend, request, source):
    ex = request.getfixturevalue(f"{backend}_executor")
    _store(settings, "mine", ["UP.US", "NEW.US"])
    ctx, _ = _ctx(settings, ex, "universes_refresh", AS_OF, ensure_days=10)
    out = ex.execute(ctx)
    assert out.status == "succeeded", out.detail
    step = out.detail["universes"]["mine"]
    assert step["refresh"] == "succeeded" and step["members"] == 2
    assert step["ensure"] == "succeeded" and step["tickers_fetched"] == 1
    assert {c[0] for c in source.price_calls} == {"NEW.US"}


def test_local_backend_refreshes_then_fills(settings, seeded, source, monkeypatch):
    import stonks.scheduling.local as local

    monkeypatch.setattr(local, "build_source", lambda source_id, sources: source)
    _store(settings, "mine", ["UP.US", "NEW.US"])
    ex = LocalExecutor()
    ctx, _ = _ctx(settings, ex, "universes_refresh", AS_OF, ensure_days=10)
    out = ex.execute(ctx)
    assert out.status == "succeeded", out.detail
    assert out.detail["universes"]["mine"]["tickers_fetched"] == 1


def test_api_tick_trades_the_members_of_a_configured_universe(settings, api_executor):
    _store(settings, "mine", ["UP.US"])
    refresh, _ = _ctx(settings, api_executor, "universes_refresh", AS_OF, ensure=False)
    assert api_executor.execute(refresh).status == "succeeded"
    settings.production.universe = "mine"
    ctx, _ = _ctx(settings, api_executor, "tick", date(2026, 3, 23))
    out = api_executor.execute(ctx)
    assert out.status == "succeeded", out.detail


def test_tick_with_an_unrefreshed_universe_skips(settings, api_executor):
    settings.production.universe = "never"
    ctx, _ = _ctx(settings, api_executor, "tick", date(2026, 3, 23))
    out = api_executor.execute(ctx)
    assert out.status == "skipped" and out.detail["reason"] == "empty_universe"
