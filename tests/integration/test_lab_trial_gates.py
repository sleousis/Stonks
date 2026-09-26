"""A real lab run hands its trials to the deflated Sharpe and PBO tests
through ``bind_run`` (BL-14, BL-15)."""

from __future__ import annotations

from datetime import date

from stonks.config import Settings
from stonks.lab.dataset import LabDataset
from stonks.lab.objectives import SharpeObjective
from stonks.lab.runner import LabRunner
from stonks.lab.survival.base import SurvivalSuite
from stonks.lab.survival.deflated_sharpe import DeflatedSharpeTest
from stonks.lab.survival.pbo import PBOTest
from stonks.lab.trials import TrialLedger
from stonks.lab.tuning.random import RandomTuner
from stonks.store.state import SqliteState
from stonks.strategies.examples.momentum import Momentum


def _dataset(lake):
    return LabDataset(
        lake=lake,
        universe=["UP.US", "DOWN.US", "FLAT.US"],
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
        train_ratio=0.6,
    )


def _runner(ledger, budget=10):
    return LabRunner(
        tuner=RandomTuner(seed=3),
        objective=SharpeObjective(),
        suite=SurvivalSuite([DeflatedSharpeTest(), PBOTest(n_blocks=8)]),
        budget=budget,
        ledger=ledger,
        settings=Settings(),
    )


def test_trial_gates_see_the_run_and_prior_runs(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    ledger = TrialLedger(state, tmp_path / "artifacts")
    ds = _dataset(lake_trending)

    first = _runner(ledger).run(Momentum, ds)
    second = _runner(ledger).run(Momentum, ds)

    assert first.trial_matrix is not None
    reports = {r.test_id: r for r in second.survival_reports}
    dsr, pbo = reports["deflated_sharpe"], reports["pbo"]

    assert "bind_run" not in dsr.notes and "per-bar" not in dsr.notes
    assert dsr.metrics["n_trials"] == second.n_trials_class == 2 * second.n_trials_run
    assert dsr.metrics["n_trials_run"] == second.n_trials_run
    assert 0.0 <= dsr.metrics["dsr"] <= 1.0
    assert dsr.metrics["dsr"] <= dsr.metrics["psr0"]

    assert pbo.metrics["n_trials"] == second.n_trials_run
    assert pbo.metrics["n_bars"] == second.trial_matrix.values.shape[0]
    if pbo.metrics["n_trials_usable"] >= 8:
        assert 0.0 <= pbo.metrics["pbo"] <= 1.0
        assert pbo.passed is (pbo.metrics["pbo"] <= 0.2)
    else:
        assert pbo.passed is False and "insufficient data" in pbo.notes
    state.close()


def test_trial_gates_fail_loudly_without_a_ledger_or_enough_trials(lake_trending):
    result = _runner(None, budget=3).run(Momentum, _dataset(lake_trending))
    pbo = next(r for r in result.survival_reports if r.test_id == "pbo")
    assert pbo.passed is False
    assert "insufficient data" in pbo.notes
    dsr = next(r for r in result.survival_reports if r.test_id == "deflated_sharpe")
    assert dsr.metrics["n_trials"] == result.n_trials_run == 3
