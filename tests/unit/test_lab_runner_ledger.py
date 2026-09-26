"""LabRunner records every run in the trial ledger (BL-04) with a manifest (BL-06)."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
import pytest

from stonks.core.protocols import SurvivalReport, TunerResult
from stonks.lab.dataset import LabDataset
from stonks.lab.runner import LabRunner
from stonks.lab.survival.base import SurvivalSuite
from stonks.lab.trials import LabRunContext, TrialLedger
from stonks.registry.artifact import ArtifactBundle
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold

CLASS_PATH = f"{BuyAndHold.__module__}:{BuyAndHold.__name__}"


@dataclass
class _Tuned:  # TunerResult plus BL-07's duck-typed ``trials``
    best_params: dict
    best_score: float
    history: list
    trials: list | None = None


@dataclass
class _Outcome:
    score: float
    returns: object
    n_bars: int


class _Tuner:
    seed = 11

    def __init__(self, n=3, fail_last=False, with_returns=False, ledger=None):
        self.n, self.fail_last, self.with_returns, self.ledger = n, fail_last, with_returns, ledger
        self.open_runs_seen: list[dict] = []

    def tune(self, strategy_cls, param_space, objective, dataset, budget):
        if self.ledger is not None:  # pre-registration must already be on disk
            self.open_runs_seen = [r for r in self.ledger.runs() if r["finished_at"] is None]
        history = []
        for i in range(self.n):
            score = float("nan") if self.fail_last and i == self.n - 1 else 0.1 * (i + 1)
            history.append(({"ticker": "X.US", "allocation": 0.1 * (i + 1)}, score))
        best = history[0]
        trials = None
        if self.with_returns:
            idx = pd.date_range("2024-01-01", periods=20, freq="D")
            rng = np.random.default_rng(0)
            trials = [
                _Outcome(s, pd.Series(rng.normal(0, 0.01, 20), index=idx), 20) for _, s in history
            ]
        return _Tuned(best[0], best[1], history, trials)


class _Objective:
    name = "fake"
    direction = "maximize"

    def score(self, strategy, dataset):
        return 0.0


class _BindRunTest:
    id = "bind_run"

    def __init__(self):
        self.ctx: LabRunContext | None = None
        self.ran_after_bind = False

    def bind_run(self, ctx):
        self.ctx = ctx

    def run(self, strategy, context):
        self.ran_after_bind = self.ctx is not None
        return SurvivalReport(test_id=self.id, passed=True, metrics={})


class _FailingTest:
    id = "fails"

    def run(self, strategy, context):
        return SurvivalReport(test_id=self.id, passed=False, metrics={})


class _Boom:
    def tune(self, *a, **k):
        raise RuntimeError("tuner exploded")


def _ds():
    return LabDataset(lake=None, universe=["X.US"], start=date(2024, 1, 1), end=date(2024, 6, 1))


@pytest.fixture
def ledger(tmp_path):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    yield TrialLedger(state, tmp_path / "artifacts")
    state.close()


def _runner(tuner, tests=(), ledger=None, **kw):
    return LabRunner(
        tuner=tuner,
        objective=_Objective(),
        suite=SurvivalSuite(list(tests)),
        budget=3,
        ledger=ledger,
        **kw,
    )


def test_result_carries_trial_data_without_a_ledger():
    result = _runner(_Tuner(n=3)).run(BuyAndHold, _ds())
    assert result.run_id.startswith("lab_")
    assert len(result.history) == 3
    assert result.n_trials_run == 3
    assert result.n_trials_class == 3
    assert [t.trial_index for t in result.trials] == [0, 1, 2]
    assert result.trial_matrix is None
    assert result.manifest["seeds"]["tuner"] == 11


def test_run_writes_trials_and_pre_registration(ledger):
    tuner = _Tuner(n=4, ledger=ledger)
    result = _runner(tuner, ledger=ledger).run(
        BuyAndHold, _ds(), hypothesis="markets rise", premortem="survivorship"
    )
    # The pre-registration row existed before the tuner ran.
    assert [r["hypothesis"] for r in tuner.open_runs_seen] == ["markets rise"]
    run = ledger.run(result.run_id)
    assert run["strategy_class"] == CLASS_PATH
    assert run["hypothesis"] == "markets rise" and run["premortem"] == "survivorship"
    assert run["tuner"] == "_Tuner" and run["objective"] == "fake"
    assert run["budget"] == 3 and run["seed"] == 11
    assert run["verdict"] == "pass" and run["finished_at"]
    assert run["manifest"]["seeds"]["tuner"] == 11
    assert run["dataset"]["universe"] == ["X.US"]
    assert len(ledger.trials(result.run_id)) == 4


def test_run_writes_a_t_by_n_matrix_when_the_tuner_reports_returns(ledger):
    result = _runner(_Tuner(n=3, with_returns=True), ledger=ledger).run(BuyAndHold, _ds())
    assert ledger.trial_matrix(result.run_id).values.shape == (20, 3)
    assert result.trial_matrix.values.shape == (20, 3)
    assert all(t.n_bars == 20 for t in ledger.trials(result.run_id))


def test_failed_trials_are_stored_as_failed(ledger):
    result = _runner(_Tuner(n=3, fail_last=True), ledger=ledger).run(BuyAndHold, _ds())
    trials = ledger.trials(result.run_id)
    assert trials[2].status == "failed" and math.isnan(trials[2].score)


def test_two_runs_of_one_class_count_2n(ledger):
    _runner(_Tuner(n=5), ledger=ledger).run(BuyAndHold, _ds())
    second = _runner(_Tuner(n=5), ledger=ledger).run(BuyAndHold, _ds())
    assert ledger.n_trials(CLASS_PATH) == 10
    assert second.n_trials_run == 5
    assert second.n_trials_class == 10


def test_bind_run_is_called_before_the_suite_runs(ledger):
    test = _BindRunTest()
    result = _runner(_Tuner(n=2, with_returns=True), [test], ledger=ledger).run(BuyAndHold, _ds())
    assert test.ran_after_bind
    ctx = test.ctx
    assert ctx.run_id == result.run_id
    assert ctx.ledger is ledger
    assert ctx.setup.budget == 3
    assert ctx.n_trials_run == 2 and ctx.n_trials_class == 2
    assert ctx.trial_matrix.values.shape == (20, 2)


def test_verdict_fail_is_recorded(ledger):
    result = _runner(_Tuner(), [_FailingTest()], ledger=ledger).run(BuyAndHold, _ds())
    assert ledger.run(result.run_id)["verdict"] == "fail"


def test_a_crashing_run_is_recorded_as_error(ledger):
    with pytest.raises(RuntimeError, match="exploded"):
        _runner(_Boom(), ledger=ledger).run(BuyAndHold, _ds(), hypothesis="h")
    (run,) = ledger.runs()
    assert run["verdict"] == "error" and run["hypothesis"] == "h"


def test_artifact_meta_lands_in_meta_json(ledger, tmp_path):
    result = _runner(_Tuner(n=3), ledger=ledger).run(
        BuyAndHold, _ds(), hypothesis="h1", premortem="p1"
    )
    ArtifactBundle(
        path=tmp_path / "art", class_path=CLASS_PATH, params={}, meta=result.artifact_meta
    ).save()
    meta = json.loads((tmp_path / "art" / "meta.json").read_text())
    assert meta["lab_run_id"] == result.run_id
    assert meta["n_trials_total"] == 3
    assert meta["hypothesis"] == "h1" and meta["premortem"] == "p1"
    assert meta["manifest"]["seeds"]["tuner"] == 11
    assert "config_hash" in meta["manifest"]


def test_plain_tuner_result_still_works(ledger):
    class _Plain:
        def tune(self, strategy_cls, param_space, objective, dataset, budget):
            p = {"ticker": "X.US", "allocation": 0.5}
            return TunerResult(best_params=p, best_score=1.0, history=[(p, 1.0)])

    result = _runner(_Plain(), ledger=ledger).run(BuyAndHold, _ds())
    assert result.n_trials_run == 1
    assert result.manifest["seeds"]["tuner"] is None
