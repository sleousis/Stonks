"""Roadmap 22.6: the ``model_retrain`` scheduler action on the ``api``,
``in_process`` and ``local`` backends, each against real stores."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.registry.store import StrategyRegistry
from stonks.registry.versions import ModelVersionRegistry
from stonks.scheduling.api_backend import API_ACTIONS
from stonks.scheduling.config import default_jobs
from stonks.scheduling.in_process import IN_PROCESS_ACTIONS, InProcessExecutor
from stonks.scheduling.jobs import SCHEDULER_ACTOR, retrain_outcome
from stonks.scheduling.local import LOCAL_ACTIONS, LocalExecutor
from stonks.store.state import SqliteState
from tests.fixtures.lifecycle import MeanFit
from tests.integration.app.test_scheduling_backends import _ctx, api_executor  # noqa: F401

FIRE = date(2026, 3, 18)


@pytest.fixture
def ml(settings, seeded) -> str:
    with SqliteState(settings.state.path) as state:
        registry = StrategyRegistry(state=state, artifacts_dir=settings.registry.artifacts_dir)
        return registry.register(MeanFit({"ticker": "UP.US"}), reports=[], strategy_id="mf")


def _versions(settings) -> dict[int, tuple[str, str]]:
    with SqliteState(settings.state.path) as state:
        registry = StrategyRegistry(state=state, artifacts_dir=settings.registry.artifacts_dir)
        return {
            v.version: (v.status, v.created_by)
            for v in ModelVersionRegistry.on(registry).list("mf")
        }


def test_every_backend_registers_the_action():
    for actions in (API_ACTIONS, IN_PROCESS_ACTIONS, LOCAL_ACTIONS):
        assert "model_retrain" in actions.names()
    job = next(j for j in default_jobs() if j.action == "model_retrain")
    assert job.trigger.type == "daily"


def test_outcome_classification():
    assert retrain_outcome({"candidates": 1, "failed": 0, "skipped": 0}).status == "succeeded"
    assert retrain_outcome({"candidates": 1, "failed": 1, "skipped": 0}).status == "failed"
    skipped = retrain_outcome({"candidates": 0, "failed": 0, "skipped": 2})
    assert skipped.status == "skipped" and skipped.detail["reason"] == "nothing_to_retrain"


def test_api_backend_retrains_through_the_api(settings, ml, api_executor):  # noqa: F811
    ctx, _ = _ctx(settings, api_executor, "model_retrain", FIRE, tickers=["UP.US"])
    out = api_executor.execute(ctx)
    assert out.status == "succeeded", out.detail
    assert out.detail["candidates"] == 1
    assert _versions(settings)[2][0] == "candidate"


def test_in_process_backend_retrains_on_the_job_runner(settings, ml, services):
    ex = InProcessExecutor(services)
    ctx, _ = _ctx(settings, ex, "model_retrain", FIRE, tickers=["UP.US"])
    out = ex.execute(ctx)
    assert out.status == "succeeded", out.detail
    assert _versions(settings)[2] == ("candidate", SCHEDULER_ACTOR)
    # a same-day re-run finds a fresh fit and skips
    again = ex.execute(_ctx(settings, ex, "model_retrain", FIRE, tickers=["UP.US"])[0])
    assert again.status == "skipped"


def test_local_backend_retrains_in_this_process(settings, ml):
    ex = LocalExecutor()
    ctx, _ = _ctx(settings, ex, "model_retrain", FIRE, tickers=["UP.US"])
    out = ex.execute(ctx)
    assert out.status == "succeeded", out.detail
    assert _versions(settings)[2] == ("candidate", SCHEDULER_ACTOR)
