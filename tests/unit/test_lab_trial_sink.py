"""The run-scoped trial sink (P2): every finished trial reaches the ledger
as it finishes, so a cancel, a crash or a broken pool cannot hide it."""

from __future__ import annotations

from datetime import date
from typing import Literal

import pytest

from stonks.core.protocols import TrialOutcome
from stonks.lab.dataset import LabDataset
from stonks.lab.heatmap import HeatmapOptions
from stonks.lab.parallel import ParallelSettings, task_seeds
from stonks.lab.runner import LabRunner
from stonks.lab.survival.base import SurvivalSuite
from stonks.lab.trials import TrialLedger
from stonks.lab.tuning.base import evaluate_candidates, merge_with_defaults, trial_sink
from stonks.lab.tuning.grid import GridTuner
from stonks.store.state import SqliteState
from tests.fixtures.parallel_lab import NoisyParamStrategy

_SERIAL = ParallelSettings(max_workers=1)
CLASS_PATH = f"{NoisyParamStrategy.__module__}:{NoisyParamStrategy.__name__}"


class _Cancelled(BaseException):
    """What a job cancel raises: not an ``Exception``, so no trial catches it."""


class _AB:
    name = "ab"
    direction: Literal["maximize", "minimize"] = "maximize"

    def __init__(self, stop_after: int | None = None) -> None:
        self.stop_after = stop_after
        self.calls = 0

    def score(self, strategy, dataset) -> float:
        self.calls += 1
        if self.stop_after is not None and self.calls > self.stop_after:
            raise _Cancelled()
        p = strategy.params
        return float(p["a"]) + p["b"]


def _ds() -> LabDataset:
    return LabDataset(lake=None, universe=["X.US"], start=date(2024, 1, 1), end=date(2024, 6, 1))


def _candidates(n: int) -> list[dict]:
    space = NoisyParamStrategy.parameter_spec()
    return [merge_with_defaults({"a": i, "b": 0.5}, space) for i in range(n)]


@pytest.fixture
def ledger(tmp_path):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    yield TrialLedger(state, tmp_path / "artifacts")
    state.close()


def test_the_sink_gets_each_finished_trial_with_its_seed():
    seen: list[tuple[TrialOutcome, int | None]] = []
    with trial_sink(lambda outcomes, seeds: seen.extend(zip(outcomes, seeds, strict=True))):
        out = evaluate_candidates(
            NoisyParamStrategy, _candidates(4), _AB(), _ds(), parallel=_SERIAL, root_seed=7
        )
    assert [o for o, _ in seen] == out
    assert [s for _, s in seen] == task_seeds(7, 4)


def test_no_sink_means_nothing_is_reported():
    out = evaluate_candidates(
        NoisyParamStrategy, _candidates(2), _AB(), _ds(), parallel=_SERIAL, root_seed=0
    )
    assert len(out) == 2  # and no error without a sink


def test_trials_finished_before_a_cancel_reach_the_sink():
    seen: list[TrialOutcome] = []
    with (
        trial_sink(lambda outcomes, seeds: seen.extend(outcomes)),
        pytest.raises(_Cancelled),
    ):
        evaluate_candidates(
            NoisyParamStrategy,
            _candidates(10),
            _AB(stop_after=8),
            _ds(),
            parallel=_SERIAL,
            root_seed=0,
        )
    assert len(seen) == 8


def test_a_search_inside_a_trial_is_not_reported():
    """An objective that tunes (a nested search) is not the run's search."""
    seen: list[TrialOutcome] = []

    class _Nested(_AB):
        def score(self, strategy, dataset) -> float:
            evaluate_candidates(
                NoisyParamStrategy, _candidates(3), _AB(), _ds(), parallel=_SERIAL, root_seed=1
            )
            return super().score(strategy, dataset)

    with trial_sink(lambda outcomes, seeds: seen.extend(outcomes)):
        evaluate_candidates(
            NoisyParamStrategy, _candidates(2), _Nested(), _ds(), parallel=_SERIAL, root_seed=0
        )
    assert len(seen) == 2


def _runner(objective, ledger, heatmap=None, budget=10) -> LabRunner:
    return LabRunner(
        tuner=GridTuner(grid_size=10, parallel=_SERIAL),
        objective=objective,
        suite=SurvivalSuite([]),
        budget=budget,
        ledger=ledger,
        heatmap=heatmap,
        parallel=_SERIAL,
        preflight=False,
    )


def test_a_cancel_after_8_of_10_trials_still_counts_8(ledger):
    """P2: the trials a cancelled run tried count against the class."""
    objective = _AB(stop_after=8)
    with pytest.raises(_Cancelled):
        _runner(objective, ledger).run(NoisyParamStrategy, _ds())
    assert ledger.n_trials(CLASS_PATH) >= 8
    [run] = ledger.runs()
    assert run["verdict"] == "error"


def test_a_finished_run_records_each_trial_once_with_its_seed(ledger):
    result = _runner(_AB(), ledger, budget=4).run(NoisyParamStrategy, _ds())
    stored = ledger.trials(result.run_id)
    assert [t.trial_index for t in stored] == list(range(result.n_trials_run))
    assert ledger.n_trials(CLASS_PATH) == result.n_trials_run
    assert [t.seed for t in stored] == task_seeds(0, result.n_trials_run)
    assert [t.seed for t in result.trials] == task_seeds(0, result.n_trials_run)


def test_heatmap_cells_are_recorded_once_after_the_sweep(ledger):
    result = _runner(_AB(), ledger, heatmap=HeatmapOptions(grid_size=2, fast=False), budget=4).run(
        NoisyParamStrategy, _ds()
    )
    assert result.heatmap is not None
    stored = ledger.trials(result.run_id)
    assert len(stored) == result.n_trials_run
    assert len({t.trial_index for t in stored}) == len(stored)


def test_tuning_trials_survive_a_heatmap_that_crashes(ledger, monkeypatch):
    import stonks.lab.heatmap as hm

    def boom(*a, **k):
        raise RuntimeError("sweep crashed")

    monkeypatch.setattr(hm, "sweep_heatmap", boom)
    with pytest.raises(RuntimeError, match="sweep crashed"):
        _runner(_AB(), ledger, heatmap=HeatmapOptions(grid_size=2, fast=False), budget=4).run(
            NoisyParamStrategy, _ds()
        )
    assert ledger.n_trials(CLASS_PATH) == 4
