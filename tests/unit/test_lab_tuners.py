"""Unit tests for GridTuner / RandomTuner behavior that doesn't need a lake:
fitting before scoring, budget-truncation sampling, and seed defaults."""

from __future__ import annotations

from typing import Any, Literal

import pytest

from stonks.core.params import ParameterSpec
from stonks.lab.tuning.grid import GridTuner
from stonks.lab.tuning.random import RandomTuner
from stonks.strategies.base import BaseStrategy


class _FittableStrategy(BaseStrategy):
    """Scores only make sense after fit(); records which dataset it saw."""

    id = "fittable_fake"

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(name="a", kind="int", default=0, bounds=(0, 9)),
            ParameterSpec(name="b", kind="int", default=0, bounds=(0, 9)),
        ]

    def __init__(self, params):
        super().__init__(params)
        self.fitted_on: Any = None

    def fit(self, dataset) -> None:
        self.fitted_on = dataset

    def estimate_return(self, ticker, as_of, lake):  # pragma: no cover - unused
        return None

    def decide(self, my_picks, portfolio, prices, as_of):  # pragma: no cover - unused
        return []


class _FitAwareObjective:
    """Returns a param-dependent score only for fitted strategies; unfitted
    strategies all score the same (which is the bug being guarded)."""

    name = "fit_aware"
    direction: Literal["maximize", "minimize"] = "maximize"

    def __init__(self) -> None:
        self.seen: list[_FittableStrategy] = []

    def score(self, strategy, dataset) -> float:
        self.seen.append(strategy)
        if strategy.fitted_on is not dataset:
            return 0.0
        return float(strategy.params["a"] * 10 + strategy.params["b"])


_DATASET = object()


@pytest.mark.parametrize("tuner", [GridTuner(grid_size=3), RandomTuner(seed=1)])
def test_tuner_fits_strategy_on_dataset_before_scoring(tuner):
    objective = _FitAwareObjective()
    result = tuner.tune(
        strategy_cls=_FittableStrategy,
        param_space=_FittableStrategy.parameter_spec(),
        objective=objective,
        dataset=_DATASET,
        budget=5,
    )
    assert objective.seen, "objective was never called"
    assert all(s.fitted_on is _DATASET for s in objective.seen)
    scores = [score for _, score in result.history]
    assert len(set(scores)) > 1, "fitted strategies should not all score the same"


def test_grid_tuner_counts_fit_failure_as_failed_trial():
    class _Exploding(_FittableStrategy):
        id = "exploding_fake"

        def fit(self, dataset) -> None:
            raise ValueError("boom")

    result = GridTuner(grid_size=2).tune(
        strategy_cls=_Exploding,
        param_space=_Exploding.parameter_spec(),
        objective=_FitAwareObjective(),
        dataset=_DATASET,
        budget=3,
    )
    assert len(result.history) == 3
    assert all(score != score for _, score in result.history)  # all NaN


def test_grid_tuner_budget_truncation_explores_every_axis():
    # 10 x 10 grid, budget 10: stopping itertools.product early would pin a=0.
    result = GridTuner(grid_size=10).tune(
        strategy_cls=_FittableStrategy,
        param_space=_FittableStrategy.parameter_spec(),
        objective=_FitAwareObjective(),
        dataset=_DATASET,
        budget=10,
    )
    assert len(result.history) == 10
    a_values = {params["a"] for params, _ in result.history}
    b_values = {params["b"] for params, _ in result.history}
    assert len(a_values) > 1
    assert len(b_values) > 1
    combos = [(p["a"], p["b"]) for p, _ in result.history]
    assert len(set(combos)) == len(combos), "sampled combinations must be distinct"


def test_grid_tuner_truncation_is_deterministic_for_a_seed():
    def run(seed):
        return GridTuner(grid_size=10, seed=seed).tune(
            strategy_cls=_FittableStrategy,
            param_space=_FittableStrategy.parameter_spec(),
            objective=_FitAwareObjective(),
            dataset=_DATASET,
            budget=10,
        )

    assert [p for p, _ in run(3).history] == [p for p, _ in run(3).history]


def test_grid_tuner_runs_full_grid_when_budget_allows():
    result = GridTuner(grid_size=3).tune(
        strategy_cls=_FittableStrategy,
        param_space=_FittableStrategy.parameter_spec(),
        objective=_FitAwareObjective(),
        dataset=_DATASET,
        budget=100,
    )
    assert len(result.history) == 9


def test_random_tuner_default_seed_is_reproducible():
    def run():
        return RandomTuner().tune(
            strategy_cls=_FittableStrategy,
            param_space=_FittableStrategy.parameter_spec(),
            objective=_FitAwareObjective(),
            dataset=_DATASET,
            budget=8,
        )

    assert [p for p, _ in run().history] == [p for p, _ in run().history]
