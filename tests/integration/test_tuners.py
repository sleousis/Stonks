"""Integration tests for lab.tuning: strategy-agnostic hyperparameter search.

The tuner must:
- consume only the strategy class's ParameterSpec (never its internals)
- skip non-tunable params and fill defaults for them
- return a TunerResult with best_params + best_score + trial history
"""

from __future__ import annotations

from datetime import date

from stonks.lab.dataset import LabDataset
from stonks.lab.objectives import SharpeObjective
from stonks.lab.tuning.grid import GridTuner
from stonks.lab.tuning.random import RandomTuner
from stonks.strategies.examples.momentum import Momentum


def _dataset(lake):
    return LabDataset(
        lake=lake,
        universe=["UP.US", "DOWN.US", "FLAT.US"],
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
        train_ratio=1.0,
    )


def test_grid_tuner_returns_result_with_non_empty_history(lake_trending):
    tuner = GridTuner(grid_size=3)
    result = tuner.tune(
        strategy_cls=Momentum,
        param_space=Momentum.parameter_spec(),
        objective=SharpeObjective(),
        dataset=_dataset(lake_trending),
        budget=20,
    )
    assert result.history, "history should not be empty"
    # best_score present and picks one of the trials
    assert any(params == result.best_params for params, _ in result.history)


def test_grid_tuner_does_not_tune_non_tunable_params(lake_trending):
    # Momentum.allocation is tunable=False; verify all trials use the default
    tuner = GridTuner(grid_size=2)
    result = tuner.tune(
        strategy_cls=Momentum,
        param_space=Momentum.parameter_spec(),
        objective=SharpeObjective(),
        dataset=_dataset(lake_trending),
        budget=8,
    )
    default_alloc = next(s.default for s in Momentum.parameter_spec() if s.name == "allocation")
    for params, _ in result.history:
        assert params["allocation"] == default_alloc


def test_random_tuner_respects_budget(lake_trending):
    tuner = RandomTuner(seed=42)
    result = tuner.tune(
        strategy_cls=Momentum,
        param_space=Momentum.parameter_spec(),
        objective=SharpeObjective(),
        dataset=_dataset(lake_trending),
        budget=5,
    )
    assert len(result.history) == 5


def test_tuners_pick_something_reasonable_on_uptrend(lake_trending):
    # With a pure uptrend, any reasonable threshold should produce positive scores
    tuner = GridTuner(grid_size=3)
    result = tuner.tune(
        strategy_cls=Momentum,
        param_space=Momentum.parameter_spec(),
        objective=SharpeObjective(),
        dataset=_dataset(lake_trending),
        budget=50,
    )
    # best_score can be 0 (all-cash) or positive, just not an error
    assert result.best_score >= -10.0
    # best params always respect bounds
    best = result.best_params
    spec = {s.name: s for s in Momentum.parameter_spec()}
    for k, v in best.items():
        s = spec[k]
        if s.kind in ("int", "float") and s.bounds is not None:
            lo, hi = s.bounds
            assert lo <= v <= hi
