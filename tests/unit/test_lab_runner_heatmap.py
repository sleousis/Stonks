"""LabRunner's heatmap step (22.5): the sweep runs after tuning, its cells
join the trial count before the suite sees it (P2), the winner stays the
tuner's, and the plateau verdict is laid over the map (P4)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

import pytest

from stonks.core.params import ParameterSpec
from stonks.core.protocols import SurvivalReport
from stonks.lab.dataset import LabDataset
from stonks.lab.heatmap import HeatmapOptions
from stonks.lab.parallel import ParallelSettings
from stonks.lab.runner import LabRunner
from stonks.lab.survival.base import SurvivalSuite
from stonks.lab.trials import TrialLedger
from stonks.lab.tuning.grid import GridTuner
from stonks.store.state import SqliteState
from stonks.strategies.base import BaseStrategy
from tests.fixtures.parallel_lab import NoisyParamStrategy

_SERIAL = ParallelSettings(max_workers=1)


class _AB:
    name = "ab"
    direction: Literal["maximize", "minimize"] = "maximize"

    def score(self, strategy, dataset) -> float:
        p = strategy.params
        return float(p["a"]) + p["b"]


@dataclass(frozen=True)
class _Opts:
    step: float = 0.2


class _Plateau:
    id = "plateau"
    options = _Opts()

    def __init__(self) -> None:
        self.n_trials_seen = 0

    def bind_run(self, ctx) -> None:
        self.n_trials_seen = ctx.n_trials_run

    def run(self, strategy, context) -> SurvivalReport:
        return SurvivalReport("plateau", False, {"train_ratio": 0.4}, "train ratio 0.4 < 0.7")


def _ds() -> LabDataset:
    return LabDataset(lake=None, universe=["X.US"], start=date(2024, 1, 1), end=date(2024, 6, 1))


def _runner(tests=(), ledger=None, heatmap=None) -> LabRunner:
    return LabRunner(
        tuner=GridTuner(grid_size=2, parallel=_SERIAL),
        objective=_AB(),
        suite=SurvivalSuite(list(tests)),
        budget=4,
        ledger=ledger,
        heatmap=heatmap,
        parallel=_SERIAL,
    )


@pytest.fixture
def ledger(tmp_path):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    yield TrialLedger(state, tmp_path / "artifacts")
    state.close()


def test_the_heatmap_cells_are_trials_and_the_winner_is_the_tuners(ledger):
    plain = _runner().run(NoisyParamStrategy, _ds())
    plateau = _Plateau()
    result = _runner([plateau], ledger=ledger, heatmap=HeatmapOptions(grid_size=2, fast=False)).run(
        NoisyParamStrategy, _ds()
    )
    heatmap = result.heatmap
    assert heatmap is not None
    cells = len(heatmap.x_values) * len(heatmap.y_values)
    assert result.n_trials_run == plain.n_trials_run + cells
    assert plateau.n_trials_seen == result.n_trials_run  # counted before the suite
    assert result.best_params == plain.best_params
    assert heatmap.best == {"a": plain.best_params["a"], "b": plain.best_params["b"]}
    [run] = ledger.runs()
    assert ledger.n_trials(run["strategy_class"]) == result.n_trials_run
    # the plateau verdict sits on the map
    assert heatmap.plateau is not None and heatmap.plateau.passed is False
    assert heatmap.plateau.step == 0.2
    assert result.artifact_meta["heatmap"] == heatmap.to_dict()


def test_no_heatmap_without_the_option():
    result = _runner().run(NoisyParamStrategy, _ds())
    assert result.heatmap is None
    assert "heatmap" not in result.artifact_meta


class _OneParam(BaseStrategy):
    id = "one_param_fake"

    @classmethod
    def parameter_spec(cls):
        return [ParameterSpec(name="a", kind="int", default=1, bounds=(0, 3))]

    def estimate_return(self, ticker, as_of, lake):  # pragma: no cover - unused
        return None

    def decide(self, my_picks, portfolio, prices, as_of):  # pragma: no cover - unused
        return []


class _A:
    name = "a"
    direction: Literal["maximize", "minimize"] = "maximize"

    def score(self, strategy, dataset) -> float:
        return float(strategy.params["a"])


def test_a_strategy_with_one_tunable_parameter_gets_no_heatmap():
    runner = LabRunner(
        tuner=GridTuner(grid_size=2, parallel=_SERIAL),
        objective=_A(),
        suite=SurvivalSuite([]),
        budget=2,
        heatmap=HeatmapOptions(fast=False),
        parallel=_SERIAL,
    )
    result = runner.run(_OneParam, _ds())
    assert result.heatmap is None
    assert result.n_trials_run == 2


def test_a_bad_axis_name_stops_the_run():
    with pytest.raises(ValueError, match="unknown parameter"):
        _runner(heatmap=HeatmapOptions(x="nope", fast=False)).run(NoisyParamStrategy, _ds())
