"""Stored universes wired into the tick, the lab and the scheduler
(roadmap 10.5, integration step)."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.app.errors import ValidationError
from stonks.app.ticks import TickRequest
from stonks.store.lake import DuckDBLake
from stonks.universes import UniverseDefinition, UniverseStore, refresh_universe


def _store_universe(settings, universe_id: str, tickers: list[str]) -> None:
    with DuckDBLake(settings.lake.path) as lake:
        UniverseStore(lake).save(
            UniverseDefinition(id=universe_id, kind="list", spec={"tickers": tickers})
        )
        refresh_universe(lake, universe_id, as_of=date(2026, 3, 1))


def test_tick_resolves_a_configured_universe_id(settings, seeded, fake_source, monkeypatch):
    import stonks.app.ticks as ticks_module
    from stonks.app.context import AppContext
    from stonks.app.services import Services

    seen: list[list[str]] = []
    real = ticks_module.build_tick_runtime

    def spy(settings_, universe):
        seen.append(list(universe))
        return real(settings_, universe)

    monkeypatch.setattr(ticks_module, "build_tick_runtime", spy)
    _store_universe(settings, "mine", ["UP.US", "FLAT.US"])
    settings.production.universe = "mine"
    svc = Services.create(AppContext(settings, source_factory=lambda: fake_source))
    svc.start()
    try:
        result = svc.ticks.run(TickRequest(as_of=date(2026, 3, 23), dry_run=True))
    finally:
        svc.shutdown()
    assert result.dry_run
    assert seen == [["FLAT.US", "UP.US"]]


def test_tick_with_an_unrefreshed_universe_id_is_a_validation_error(services, settings):
    settings.production.universe = "never_refreshed"
    with pytest.raises(ValidationError, match="refresh"):
        services.ticks.run(TickRequest(as_of=date(2026, 3, 23), dry_run=True))


def test_tick_keeps_explicit_tickers_over_the_universe_id(services, settings):
    settings.production.universe = "never_refreshed"
    result = services.ticks.run(
        TickRequest(as_of=date(2026, 3, 23), dry_run=True, tickers=["UP.US"])
    )
    assert result.dry_run


# ---- lab runs over a stored universe -------------------------------------------------

BAH = "stonks.strategies.examples.buy_and_hold:BuyAndHold"


def _lab_body(**overrides) -> dict:
    body = {
        "strategy": {"class_path": BAH},
        "start": "2025-10-01",
        "end": "2026-04-01",
        "budget": 1,
        "grid_size": 1,
        "survival_tests": ["oos"],
        "cost_model": "zero",
    }
    body.update(overrides)
    return body


def test_lab_run_request_needs_tickers_or_a_universe_id():
    from pydantic import ValidationError as PydanticError

    from stonks.app.lab import LabRunRequest

    assert LabRunRequest(**_lab_body(universe_id="mine")).universe == []
    assert LabRunRequest(**_lab_body(universe=["UP.US"])).universe_id is None
    with pytest.raises(PydanticError, match="universe"):
        LabRunRequest(**_lab_body())


def test_lab_run_over_a_universe_id_resolves_its_members(settings, seeded):
    from stonks.app.lab import LabRunRequest, execute_lab_run
    from stonks.strategies.examples.buy_and_hold import BuyAndHold

    _store_universe(settings, "mine", ["UP.US", "FLAT.US"])
    with DuckDBLake(settings.lake.path) as lake:
        execution = execute_lab_run(
            settings, BuyAndHold, LabRunRequest(**_lab_body(universe_id="mine")), lake=lake
        )
    dataset = execution.result.manifest["dataset"]
    assert dataset["universe_id"] == "mine"
    assert sorted(dataset["universe"]) == ["FLAT.US", "UP.US"]


def test_lab_run_with_an_unknown_universe_id_is_not_found(services):
    from stonks.app.errors import NotFoundError
    from stonks.app.lab import LabRunRequest

    with pytest.raises(NotFoundError):
        services.lab.submit_lab_run(LabRunRequest(**_lab_body(universe_id="nope")))


def test_lab_run_with_ensure_data_chains_an_ensure_job_on_the_lake_lane(settings, seeded):
    from stonks.app.context import AppContext
    from stonks.app.lab import LAB_ENSURE_JOB, LAB_RUN_JOB, LabRunRequest, LabRunView
    from stonks.app.services import Services
    from stonks.ingest.ensure import EnsureReport
    from tests.fixtures.universes import FakeListingSource, bars

    source = FakeListingSource(
        prices={"NEW.US": bars("NEW.US", date(2025, 6, 2), date(2026, 4, 1))}
    )
    _store_universe(settings, "mine", ["UP.US", "NEW.US"])
    svc = Services.create(AppContext(settings, source_factory=lambda: source))
    svc.start()
    try:
        job = svc.lab.submit_lab_run(
            LabRunRequest(**_lab_body(universe_id="mine", ensure_data=True))
        )
        final = svc.runner.wait(job.id, timeout=120)
        assert final.status == "succeeded", final.error
        ensure_jobs = svc.runner.store.list(kind=LAB_ENSURE_JOB, limit=10, offset=0).items
        assert [j.status for j in ensure_jobs] == ["succeeded"]
        assert svc.runner.is_operation(LAB_ENSURE_JOB)
        view = svc.jobs.typed_result(job.id, LAB_RUN_JOB, LabRunView)
        assert view.ensure_job_id == ensure_jobs[0].id
        report = svc.jobs.typed_result(view.ensure_job_id, LAB_ENSURE_JOB, EnsureReport)
        assert report.tickers_fetched == 1
    finally:
        svc.shutdown()
    assert {c[0] for c in source.price_calls} == {"NEW.US"}
