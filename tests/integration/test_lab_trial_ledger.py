"""A real lab run on a lake writes the ledger, the manifest and meta.json."""

from __future__ import annotations

import json
from datetime import date

from stonks.config import Settings
from stonks.lab.dataset import LabDataset
from stonks.lab.objectives import SharpeObjective
from stonks.lab.runner import LabRunner
from stonks.lab.survival.base import SurvivalSuite
from stonks.lab.survival.oos import OutOfSampleTest
from stonks.lab.trials import TrialLedger
from stonks.lab.tuning.random import RandomTuner
from stonks.registry.artifact import update_meta
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.momentum import Momentum


def test_lab_run_is_ledgered_and_reproducible(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    artifacts = tmp_path / "artifacts"
    ledger = TrialLedger(state, artifacts)
    ds = LabDataset(
        lake=lake_trending,
        universe=["UP.US", "DOWN.US", "FLAT.US"],
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
        train_ratio=0.6,
    )
    runner = LabRunner(
        tuner=RandomTuner(seed=4),
        objective=SharpeObjective(),
        suite=SurvivalSuite([OutOfSampleTest(min_sharpe=-10.0, max_drawdown_limit=-0.99)]),
        budget=4,
        ledger=ledger,
        settings=Settings(),
    )
    result = runner.run(Momentum, ds, hypothesis="trend persists", premortem="beta")

    run = ledger.run(result.run_id)
    assert run["hypothesis"] == "trend persists"
    assert run["seed"] == 4
    assert run["verdict"] == result.verdict
    assert len(ledger.trials(result.run_id)) == result.n_trials_run == len(result.history)
    fp = run["manifest"]["data_fingerprint"]
    assert set(fp["tickers"]) == {"UP.US", "DOWN.US", "FLAT.US"}
    assert run["manifest"]["config_hash"]

    registry = StrategyRegistry(state, artifacts)
    sid = registry.register(result.strategy, result.survival_reports)
    update_meta(artifacts / sid, result.artifact_meta)
    meta = json.loads((artifacts / sid / "meta.json").read_text())
    assert meta["lab_run_id"] == result.run_id
    assert meta["n_trials_total"] == result.n_trials_class
    assert meta["manifest"]["data_fingerprint"]["hash"] == fp["hash"]
    state.close()
