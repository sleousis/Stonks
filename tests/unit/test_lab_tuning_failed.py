"""A search where every trial failed has no result: ``best_of`` says NaN,
not the defaults at 0.0, and the lab run ends with a named error."""

from __future__ import annotations

import math
from datetime import date
from typing import Literal

import pytest

from stonks.config import Settings
from stonks.core.protocols import TrialOutcome
from stonks.lab.dataset import LabDataset
from stonks.lab.parallel import ParallelSettings
from stonks.lab.runner import LAB_RUN_TUNING_FAILED_MESSAGE, LabRunner
from stonks.lab.survival.base import SurvivalSuite
from stonks.lab.trials import TrialLedger
from stonks.lab.tuning.base import TuningFailed, best_of
from stonks.lab.tuning.grid import GridTuner
from stonks.store.state import SqliteState
from tests.fixtures.parallel_lab import NoisyParamStrategy

_SERIAL = ParallelSettings(max_workers=1)
_SPACE = NoisyParamStrategy.parameter_spec()


def test_best_of_is_nan_when_no_trial_scored():
    outcomes = [TrialOutcome.failed({"a": 1}, "boom"), TrialOutcome({"a": 2}, math.nan)]
    params, score = best_of(outcomes, "maximize", _SPACE)
    assert math.isnan(score)
    assert params == {"a": 0, "b": 0.0, "fail_on": -1}  # the defaults, flagged by NaN


def test_best_of_is_nan_for_an_empty_search():
    _, score = best_of([], "minimize", _SPACE)
    assert math.isnan(score)


def test_best_of_still_picks_the_best_finite_score():
    outcomes = [TrialOutcome.failed({"a": 1}, "boom"), TrialOutcome({"a": 2}, -0.5)]
    assert best_of(outcomes, "maximize", _SPACE) == ({"a": 2}, -0.5)


class _Fragile(NoisyParamStrategy):
    """Every fit raises, as a strategy reading a table missing from the lake."""

    id = "fragile_fake"

    def fit(self, dataset) -> None:
        raise RuntimeError("transient: table missing in snapshot")


class _Score:
    name = "score"
    direction: Literal["maximize", "minimize"] = "maximize"

    def score(self, strategy, dataset) -> float:
        return float(strategy.params["a"])


def _ds() -> LabDataset:
    return LabDataset(lake=None, universe=["X.US"], start=date(2024, 1, 1), end=date(2024, 6, 1))


@pytest.fixture
def ledger(tmp_path):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    yield TrialLedger(state, tmp_path / "artifacts")
    state.close()


def test_a_run_where_every_trial_fails_ends_with_tuning_failed(ledger):
    runner = LabRunner(
        GridTuner(grid_size=5, parallel=_SERIAL),
        _Score(),
        SurvivalSuite([]),
        budget=5,
        ledger=ledger,
        settings=Settings(),
        preflight=False,
        parallel=_SERIAL,
    )
    with pytest.raises(TuningFailed) as caught:
        runner.run(_Fragile, _ds())
    assert "transient: table missing in snapshot" in str(caught.value)
    assert caught.value.first_error == "transient: table missing in snapshot"
    [run] = ledger.runs()
    assert run["verdict"] == "error"
    assert run["error"] == LAB_RUN_TUNING_FAILED_MESSAGE
    assert "transient" not in run["error"]  # the raw error is for admins only
    assert run["error_detail"].startswith("TuningFailed:")
    assert "transient: table missing in snapshot" in run["error_detail"]
    # the failed trials still count (P2)
    assert len(ledger.trials(run["id"])) == 5
    assert all(t.status == "failed" for t in ledger.trials(run["id"]))
