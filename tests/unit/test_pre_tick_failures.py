"""A tick that fails before ``run_tick`` records its row never alerted (the
tick alerts only from its own error handler), so the scheduler must alert
on it instead of waiting for the deadline watchdog (review wave 2, item 8)."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from stonks.app import ticks as app_ticks
from stonks.app.ticks import TickRequest, execute_tick
from stonks.production.tick_failures import PreTickError, tick_row_count
from stonks.scheduling.api_backend import tick_job_outcome
from stonks.store.state import SqliteState


def test_a_pre_tick_error_is_not_counted_as_alerted():
    out = tick_job_outcome(
        "failed", "PreTickError: OperationalError: database is locked", None, "j"
    )
    assert out.status == "failed" and not out.alerted


def test_a_failure_inside_the_tick_stays_alerted():
    out = tick_job_outcome("failed", "RuntimeError: broker down", None, "j")
    assert out.status == "failed" and out.alerted


@pytest.fixture
def state(tmp_path):
    with SqliteState(tmp_path / "state.sqlite") as s:
        s.migrate()
        yield s


def _stub_runtime(monkeypatch, plan_for):
    runtime = SimpleNamespace(settings=None, notifier=None, broker_factory=None, plan_for=plan_for)
    monkeypatch.setattr(app_ticks, "request_universe", lambda *a, **k: ["UP.US"])
    monkeypatch.setattr(app_ticks, "build_tick_runtime", lambda *a, **k: runtime)


def test_a_plan_build_error_is_a_pre_tick_error(monkeypatch, state):
    def broken_plan(*_a, **_k):
        raise KeyError("portfolio pf_x")

    _stub_runtime(monkeypatch, broken_plan)
    with pytest.raises(PreTickError, match="KeyError"):
        execute_tick(None, state, None, None, TickRequest(dry_run=True))


def test_a_locked_database_before_the_tick_row_is_a_pre_tick_error(monkeypatch, state):
    _stub_runtime(monkeypatch, lambda *a, **k: None)

    def locked(**_kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(app_ticks, "run_tick", locked)
    with pytest.raises(PreTickError, match="database is locked"):
        execute_tick(None, state, None, None, TickRequest(dry_run=True))


def test_a_failure_after_the_tick_row_keeps_its_own_type(monkeypatch, state):
    _stub_runtime(monkeypatch, lambda *a, **k: None)

    def fails_inside(**kw):
        kw["state"].execute(
            "INSERT INTO tick_runs (id, started_at, status) VALUES ('t1', 'x', 'error')"
        )
        raise RuntimeError("broker down")

    monkeypatch.setattr(app_ticks, "run_tick", fails_inside)
    before = tick_row_count(state)
    with pytest.raises(RuntimeError, match="broker down") as info:
        execute_tick(None, state, None, None, TickRequest(dry_run=True))
    assert not isinstance(info.value, PreTickError)
    assert tick_row_count(state) == (before or 0) + 1
