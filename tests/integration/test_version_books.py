"""Roadmap 22.6: a candidate model runs as a model book beside the live one,
never trades, and swaps in only after the swap check passes."""

from __future__ import annotations

import json
from datetime import date

import pytest

from stonks.lifecycle.check import evaluate_swap
from stonks.lifecycle.retrain import retrain_models
from stonks.lifecycle.settings import ModelLifecycleSettings, SwapPolicy
from stonks.production.tick import TickSettings, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.registry.versions import ModelVersionRegistry, SwapRefused
from stonks.store.state import SqliteState
from tests.fixtures.governance import seed_status
from tests.fixtures.lifecycle import MeanFit

DAYS = [date(2026, 3, 18), date(2026, 3, 19), date(2026, 3, 20)]
SETTINGS = TickSettings(universe=["UP.US", "FLAT.US", "DOWN.US"], initial_cash=10_000.0)
LIFECYCLE = ModelLifecycleSettings(lookback_days=60)


@pytest.fixture
def env(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    sid = registry.register(MeanFit({"ticker": "UP.US"}), reports=[])
    seed_status(registry, sid, "active")
    yield lake_trending, state, registry, sid
    state.close()


def _summary(state, tick_id):
    row = state.sql("SELECT summary_json FROM tick_runs WHERE id = ?", [tick_id])[0]
    return json.loads(row["summary_json"])


def _retrain(env):
    lake, state, registry, _ = env
    retrain_models(
        state, lake, registry, LIFECYCLE, as_of=DAYS[0], universe=["UP.US"], actor="test",
        max_workers=1,
    )  # fmt: skip


def test_no_candidate_keeps_no_version_book(env):
    lake, state, registry, _ = env
    result = run_tick(state, lake, registry, SETTINGS, as_of=DAYS[0])
    assert "model_versions" not in _summary(state, result.tick_id)
    assert state.sql("SELECT * FROM model_version_snapshots") == []


def test_candidate_and_live_run_as_model_books(env):
    lake, state, registry, sid = env
    _retrain(env)
    for day in DAYS:
        result = run_tick(state, lake, registry, SETTINGS, as_of=day)
    summary = _summary(state, result.tick_id)
    books = {o["strategy_id"]: o["status"] for o in summary["model_versions"]}
    assert books == {f"{sid}@v1": "evaluated", f"{sid}@v2": "evaluated"}

    # only the live strategy trades in the real ledger, once
    orders = state.sql("SELECT strategy_id, ticker FROM orders")
    assert [(r["strategy_id"], r["ticker"]) for r in orders] == [(sid, "UP.US")]
    snaps = state.sql("SELECT version, COUNT(*) AS n FROM model_version_snapshots GROUP BY version")
    assert {r["version"]: r["n"] for r in snaps} == {1: 3, 2: 3}
    buys = state.sql("SELECT version, ticker, side FROM model_version_decisions ORDER BY version")
    assert [(r["version"], r["ticker"], r["side"]) for r in buys] == [
        (1, "UP.US", "buy"),
        (2, "UP.US", "buy"),
    ]


def test_swap_check_gates_the_swap(env):
    lake, state, registry, sid = env
    _retrain(env)
    versions = ModelVersionRegistry.on(registry)
    early = evaluate_swap(state, versions, sid, 2, SwapPolicy(min_days=2))
    assert not early.passed
    assert {c.name for c in early.checks if not c.passed} == {
        "min_days",
        "max_drawdown",
        "vs_live",
    }
    with pytest.raises(SwapRefused):
        versions.swap(sid, 2, actor="test", check_report=early)

    for day in DAYS:
        run_tick(state, lake, registry, SETTINGS, as_of=day)
    report = evaluate_swap(state, versions, sid, 2, SwapPolicy(min_days=2))
    assert report.passed, report.as_dict()
    assert report.days == 3 and report.live_version == 1
    assert report.candidate_return == pytest.approx(report.live_return)

    versions.swap(sid, 2, actor="test", check_report=report)
    assert registry.load(sid).mean_close is not None  # type: ignore[attr-defined]
    # no candidate left: the next tick keeps no version book
    nxt = run_tick(state, lake, registry, SETTINGS, as_of=date(2026, 3, 23))
    assert "model_versions" not in _summary(state, nxt.tick_id)


def test_rejected_candidate_stops_its_book(env):
    lake, state, registry, sid = env
    _retrain(env)
    ModelVersionRegistry.on(registry).reject(sid, 2, actor="test", reason="bad fit")
    result = run_tick(state, lake, registry, SETTINGS, as_of=DAYS[0])
    assert "model_versions" not in _summary(state, result.tick_id)
