"""Roadmap 23.9: ``lab verify`` reruns a stored lab result and reports
whether a restatement moved it."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.backtest.costs import CostModelSettings
from stonks.config import Settings
from stonks.lab.dataset import LabDataset
from stonks.lab.objectives import SharpeObjective
from stonks.lab.runner import LabRunner
from stonks.lab.survival.base import SurvivalSuite
from stonks.lab.survival.oos import OutOfSampleTest
from stonks.lab.trials import TrialLedger
from stonks.lab.tuning.random import RandomTuner
from stonks.lab.verify import resolve_target, verify
from stonks.registry.artifact import update_meta
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.momentum import Momentum


@pytest.fixture
def run(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    artifacts = tmp_path / "artifacts"
    ds = LabDataset(
        lake=lake_trending,
        universe=["UP.US", "DOWN.US", "FLAT.US"],
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
        train_ratio=0.6,
        costs=CostModelSettings(),
    )
    runner = LabRunner(
        tuner=RandomTuner(seed=4),
        objective=SharpeObjective(),
        suite=SurvivalSuite([OutOfSampleTest(min_sharpe=-10.0, max_drawdown_limit=-0.99)]),
        budget=4,
        ledger=TrialLedger(state, artifacts),
        settings=Settings(),
    )
    result = runner.run(Momentum, ds, hypothesis="trend persists")
    registry = StrategyRegistry(state, artifacts)
    sid = registry.register(result.strategy, result.survival_reports)
    update_meta(artifacts / sid, result.artifact_meta)
    yield state, registry, artifacts, result, sid, lake_trending
    state.close()


def test_an_untouched_run_reproduces(run):
    state, registry, artifacts, result, sid, lake = run
    for key in (result.run_id, sid):
        target = resolve_target(state, registry, artifacts, key)
        report = verify(target, lake=lake, settings=Settings(), tolerance=1e-9)
        assert report.error is None, report.error
        assert report.data_changed is False and report.changed_tickers == []
        assert report.config_changed is False
        assert report.stored_score == pytest.approx(result.best_score)
        assert report.current_score == pytest.approx(result.best_score)
        assert report.moved is False
    assert resolve_target(state, registry, artifacts, sid).kind == "strategy"


def test_a_restated_bar_is_found_and_moves_the_result(run):
    state, registry, artifacts, result, sid, lake = run
    lake.con.execute(
        "UPDATE bars SET close = close * 1.5, adj_close = adj_close * 1.5"
        " WHERE ticker = 'UP.US' AND timestamp >= '2025-11-01' AND timestamp < '2025-12-01'"
    )
    target = resolve_target(state, registry, artifacts, result.run_id)
    report = verify(target, lake=lake, settings=Settings(), tolerance=1e-6)
    assert report.data_changed is True
    assert report.changed_tickers == ["UP.US"]
    assert report.current_score != pytest.approx(result.best_score)
    assert report.moved is True
    assert report.as_dict()["score_delta"] == pytest.approx(report.score_delta)


def test_unknown_target(run):
    state, registry, artifacts, *_ = run
    with pytest.raises(KeyError):
        resolve_target(state, registry, artifacts, "nope")
