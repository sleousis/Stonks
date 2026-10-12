"""``lab verify`` rescores the trial the tuner chose, seeded as it ran, on
the dataset the run used (roadmap 23.9, P2 and P12).

A heatmap appends its cells to the run's trials, so the best trial is not
always the tuner's winner. A run with lots, shorting, a fill model or an
embargo backtests with them, so the rebuilt dataset must too, or verify
raises a false alarm.
"""

from __future__ import annotations

from datetime import date

import pytest

from stonks.backtest.costs import CostModelSettings
from stonks.backtest.fills import ExecutionSettings
from stonks.backtest.shorting import ShortingSettings
from stonks.config import Settings
from stonks.lab import verify as verify_mod
from stonks.lab.backtesting import backtest_config, lab_broker
from stonks.lab.dataset import LabDataset
from stonks.lab.heatmap import HeatmapOptions
from stonks.lab.manifest import build_manifest
from stonks.lab.objectives import SharpeObjective
from stonks.lab.parallel import ParallelSettings
from stonks.lab.runner import LabRunner
from stonks.lab.survival.base import SurvivalSuite
from stonks.lab.survival.oos import OutOfSampleTest
from stonks.lab.trials import TrialLedger
from stonks.lab.tuning.grid import GridTuner
from stonks.lab.tuning.random import RandomTuner
from stonks.lab.verify import _dataset, resolve_target, verify
from stonks.portfolio.lots import LotSettings
from stonks.portfolio.settings import ConstructionSettings
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.momentum import Momentum

SERIAL = ParallelSettings(max_workers=1)
UNIVERSE = ["UP.US", "DOWN.US", "FLAT.US"]


def _ds(lake, **kw) -> LabDataset:
    return LabDataset(
        lake=lake,
        universe=list(UNIVERSE),
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
        train_ratio=0.6,
        costs=CostModelSettings(),
        **kw,
    )


def _suite() -> SurvivalSuite:
    return SurvivalSuite([OutOfSampleTest(min_sharpe=-10.0, max_drawdown_limit=-0.99)])


@pytest.fixture
def ledger(tmp_path):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    artifacts = tmp_path / "artifacts"
    yield state, artifacts
    state.close()


def test_verify_rescores_the_tuner_winner_not_a_heatmap_cell(ledger, lake_trending):
    state, artifacts = ledger
    runner = LabRunner(
        tuner=GridTuner(grid_size=5, seed=3, parallel=SERIAL),
        objective=SharpeObjective(),
        suite=_suite(),
        budget=1,
        ledger=TrialLedger(state, artifacts),
        settings=Settings(),
        heatmap=HeatmapOptions(x="lookback_days", y="threshold", grid_size=4, fast=False),
        parallel=SERIAL,
    )
    result = runner.run(Momentum, _ds(lake_trending), fixed_params={"skip_days": 0})
    assert result.heatmap is not None
    target = resolve_target(state, StrategyRegistry(state, artifacts), artifacts, result.run_id)
    assert target.trial_index == result.best_trial_index
    assert all(target.params.get(k) == v for k, v in result.best_params.items())
    assert target.stored_score == pytest.approx(result.best_score)
    report = verify(target, lake=lake_trending, settings=Settings(), tolerance=1e-9)
    assert report.error is None, report.error
    assert report.current_score == pytest.approx(result.best_score)
    assert report.moved is False


def test_an_old_run_without_a_stored_winner_falls_back_to_the_best_trial(ledger, lake_trending):
    state, artifacts = ledger
    runner = LabRunner(
        tuner=RandomTuner(seed=4, parallel=SERIAL),
        objective=SharpeObjective(),
        suite=_suite(),
        budget=3,
        ledger=TrialLedger(state, artifacts),
        settings=Settings(),
    )
    result = runner.run(Momentum, _ds(lake_trending))
    state.execute(
        "UPDATE lab_runs SET best_trial_index = NULL, best_params_json = NULL WHERE id = ?",
        [result.run_id],
    )
    target = resolve_target(state, StrategyRegistry(state, artifacts), artifacts, result.run_id)
    assert target.stored_score == pytest.approx(result.best_score)
    report = verify(target, lake=lake_trending, settings=Settings(), tolerance=1e-9)
    assert report.moved is False, report.as_dict()


def test_rescore_uses_the_stored_trial_seed(ledger, lake_trending, monkeypatch):
    state, artifacts = ledger
    runner = LabRunner(
        tuner=RandomTuner(seed=4, parallel=SERIAL),
        objective=SharpeObjective(),
        suite=_suite(),
        budget=2,
        ledger=TrialLedger(state, artifacts),
        settings=Settings(),
    )
    result = runner.run(Momentum, _ds(lake_trending))
    state.execute(
        "UPDATE lab_trials SET seed = 987654 WHERE run_id = ? AND trial_index = ?",
        [result.run_id, result.best_trial_index],
    )
    target = resolve_target(state, StrategyRegistry(state, artifacts), artifacts, result.run_id)
    assert target.trial_seed == 987654
    import stonks.lab.parallel as parallel

    seen: list[int | None] = []
    real = parallel._call_seeded

    def spy(fn, state_, task, seed):
        seen.append(seed)
        return real(fn, state_, task, seed)

    monkeypatch.setattr(parallel, "_call_seeded", spy)
    verify(target, lake=lake_trending, settings=Settings(), tolerance=1e-9)
    assert seen == [987654]


def test_an_untouched_run_with_lots_does_not_move(ledger, lake_trending):
    state, artifacts = ledger
    lots = LotSettings(profile="whole", lot_sizes=dict.fromkeys(UNIVERSE, 1e6))
    runner = LabRunner(
        tuner=RandomTuner(seed=4, parallel=SERIAL),
        objective=SharpeObjective(),
        suite=_suite(),
        budget=3,
        ledger=TrialLedger(state, artifacts),
        settings=Settings(),
    )
    result = runner.run(
        Momentum, _ds(lake_trending, lots=lots), fixed_params={"threshold": 0.0, "skip_days": 0}
    )
    assert result.manifest.get("lots")
    target = resolve_target(state, StrategyRegistry(state, artifacts), artifacts, result.run_id)
    report = verify(target, lake=lake_trending, settings=Settings(), tolerance=1e-9)
    assert report.error is None, report.error
    assert report.current_score == pytest.approx(result.best_score)
    assert report.moved is False


def test_the_dataset_round_trips_through_the_manifest(lake_trending):
    ds = _ds(
        lake_trending,
        lots=LotSettings(profile="whole_shares"),
        shorting=ShortingSettings(borrow_multiplier=2.0),
        execution=ExecutionSettings(settlement_days=1),
        construction=ConstructionSettings(method="equal_weight_top_n", params={"n": 2}),
        benchmark="EW",
        embargo_bars=5,
    )
    rebuilt = _dataset(build_manifest(Settings(), ds, {}), lake_trending, Settings())
    for window in (ds.train_window, ds.val_window, ds.full_window):
        assert backtest_config(rebuilt, window) == backtest_config(ds, window)
    assert rebuilt.train_window == ds.train_window
    assert rebuilt.val_window == ds.val_window
    assert rebuilt.benchmark == ds.benchmark
    assert rebuilt.embargo_bars == ds.embargo_bars
    assert rebuilt.costs == ds.costs
    assert (rebuilt.execution, rebuilt.shorting) == (ds.execution, ds.shorting)
    assert type(lab_broker(rebuilt)) is type(lab_broker(ds))


def test_a_plain_dataset_round_trips_without_the_settings_lots(lake_trending):
    """A new manifest with no lots means fractional, whatever the settings say."""
    ds = _ds(lake_trending)
    settings = Settings()
    settings.backtest.lots = LotSettings(profile="whole_shares")
    rebuilt = _dataset(build_manifest(Settings(), ds, {}), lake_trending, settings)
    assert backtest_config(rebuilt, ds.train_window) == backtest_config(ds, ds.train_window)


def test_an_old_manifest_falls_back_to_the_settings(lake_trending):
    ds = _ds(lake_trending)
    manifest = build_manifest(Settings(), ds, {})
    manifest.pop("backtest")
    settings = Settings()
    settings.backtest.execution = ExecutionSettings(settlement_days=2)
    settings.lab.embargo_bars = 3
    rebuilt = verify_mod._dataset(manifest, lake_trending, settings)
    assert rebuilt.execution == ExecutionSettings(settlement_days=2)
    assert rebuilt.embargo_bars == 3


def test_verify_knows_every_registered_objective(ledger, lake_trending):
    """A run scored by a ``cv_`` objective (or a plugin objective) is
    rescored through the objective registry, not a fixed list."""
    from dataclasses import replace

    state, artifacts = ledger
    runner = LabRunner(
        tuner=GridTuner(grid_size=2, parallel=SERIAL),
        objective=SharpeObjective(),
        suite=_suite(),
        budget=2,
        ledger=TrialLedger(state, artifacts),
        settings=Settings(),
    )
    result = runner.run(Momentum, _ds(lake_trending))
    target = resolve_target(state, StrategyRegistry(state, artifacts), artifacts, result.run_id)
    score = verify_mod._rescore(
        replace(target, objective="cv_sharpe"), lake_trending, Settings(), None
    )
    assert isinstance(score, float)
    assert verify_mod._direction("cv_sharpe") == "maximize"
