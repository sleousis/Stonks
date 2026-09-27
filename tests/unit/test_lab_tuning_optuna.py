"""The Optuna tuner (22.1, ``lab/tuning/optuna.py``): seeded, parallel
through the lab pool, every trial (pruned ones too) in the result so the
trial ledger counts it (P2)."""

from __future__ import annotations

import math
from typing import Any, Literal

import pytest

from stonks.core.params import ParameterSpec
from stonks.core.protocols import TrialOutcome, Tuner
from stonks.lab.dataset import LabDataset
from stonks.lab.parallel import ParallelSettings
from stonks.lab.trials import trials_from_tuning
from stonks.lab.tuning.optuna import PRUNED, OptunaTuner
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples.momentum import Momentum
from tests.fixtures.parallel_lab import GlobalRngObjective, NoisyParamStrategy, random_walk_lake

_SERIAL = ParallelSettings(max_workers=1)


class PeakObjective:
    """A smooth score with one peak at a=7, b=0.3."""

    name = "peak"
    direction: Literal["maximize", "minimize"] = "maximize"

    def score(self, strategy, dataset) -> float:
        p = strategy.params
        return -((p["a"] - 7) ** 2) - 10.0 * (p["b"] - 0.3) ** 2


class ValleyObjective(PeakObjective):
    name = "valley"
    direction: Literal["maximize", "minimize"] = "minimize"

    def score(self, strategy, dataset) -> float:
        return -super().score(strategy, dataset)


def _tune(tuner, objective=None, budget=12, strategy=NoisyParamStrategy, dataset=None):
    return tuner.tune(
        strategy_cls=strategy,
        param_space=strategy.parameter_spec(),
        objective=objective or PeakObjective(),
        dataset=dataset,
        budget=budget,
    )


def test_it_is_a_tuner():
    assert isinstance(OptunaTuner(), Tuner)


def test_every_trial_is_in_the_result_and_the_budget_holds():
    result = _tune(OptunaTuner(seed=1, parallel=_SERIAL, batch_size=4), budget=10)
    assert len(result.history) == len(result.trials or []) == 10
    records, _ = trials_from_tuning(result)
    assert len(records) == 10


def test_the_same_seed_gives_the_same_trials_and_another_seed_does_not():
    a = _tune(OptunaTuner(seed=3, parallel=_SERIAL), budget=14)
    b = _tune(OptunaTuner(seed=3, parallel=_SERIAL), budget=14)
    c = _tune(OptunaTuner(seed=4, parallel=_SERIAL), budget=14)
    assert a.history == b.history
    assert a.history != c.history


def test_the_worker_count_does_not_change_the_result():
    # global RNG draws in the objective: reproducible only when every trial
    # is seeded by its index, whatever process runs it
    serial = _tune(OptunaTuner(seed=2, parallel=_SERIAL, batch_size=4), GlobalRngObjective(), 8)
    pooled = _tune(
        OptunaTuner(seed=2, parallel=ParallelSettings(max_workers=2), batch_size=4),
        GlobalRngObjective(),
        8,
    )
    assert serial.trials == pooled.trials


def test_tpe_finds_the_peak_region():
    result = _tune(OptunaTuner(seed=0, parallel=_SERIAL, batch_size=5), budget=40)
    assert abs(result.best_params["a"] - 7) <= 1
    assert abs(result.best_params["b"] - 0.3) <= 0.2
    assert result.best_score == max(s for _, s in result.history)


def test_a_minimising_objective_picks_the_lowest_score():
    result = _tune(OptunaTuner(seed=0, parallel=_SERIAL), ValleyObjective(), budget=15)
    assert result.best_score == min(s for _, s in result.history)


def test_params_are_plain_python_values_within_the_spec():
    result = _tune(OptunaTuner(seed=5, parallel=_SERIAL), budget=6)
    for params, _ in result.history:
        assert type(params["a"]) is int and 0 <= params["a"] <= 9
        assert type(params["b"]) is float and 0.0 <= params["b"] <= 1.0
        assert params["fail_on"] == -1  # not tunable: the default


class _Mixed(BaseStrategy):
    id = "mixed_fake"

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(name="mode", kind="categorical", default="x", bounds=["x", "y", "z"]),
            ParameterSpec(name="flag", kind="bool", default=False),
            ParameterSpec(name="ticker", kind="categorical", default="A.US", bounds=None),
        ]

    def estimate_return(self, ticker, as_of, lake):  # pragma: no cover - unused
        return None

    def decide(self, my_picks, portfolio, prices, as_of):  # pragma: no cover - unused
        return []


class _MixedObjective:
    name = "mixed"
    direction: Literal["maximize", "minimize"] = "maximize"

    def score(self, strategy, dataset) -> float:
        p = strategy.params
        return {"x": 0.0, "y": 1.0, "z": 2.0}[p["mode"]] + (0.5 if p["flag"] else 0.0)


def test_categorical_and_bool_specs_are_sampled_from_their_choices():
    result = _tune(OptunaTuner(seed=0, parallel=_SERIAL), _MixedObjective(), 12, _Mixed)
    for params, _ in result.history:
        assert params["mode"] in ("x", "y", "z")
        assert type(params["flag"]) is bool
        assert params["ticker"] == "A.US"  # an open categorical keeps its default


def test_a_failing_trial_is_recorded_as_failed_and_the_search_goes_on():
    space = [
        ParameterSpec(name="a", kind="int", default=0, bounds=(0, 1)),
        ParameterSpec(name="b", kind="float", default=0.0, bounds=(0.0, 1.0)),
        ParameterSpec(name="fail_on", kind="int", default=1, bounds=(-1, 9), tunable=False),
    ]
    result = OptunaTuner(seed=0, parallel=_SERIAL).tune(
        NoisyParamStrategy, space, PeakObjective(), None, budget=10
    )
    failed = [t for t in result.trials or [] if t.status == "failed"]
    assert failed and all(t.params["a"] == 1 for t in failed)
    assert len(result.trials or []) == 10
    assert result.best_params["a"] == 0


class _MultiObjective:
    """Two components: ``up`` to maximise and ``down`` to minimise."""

    name = "multi_fake"
    direction: Literal["maximize", "minimize"] = "maximize"
    metric_names = ["up", "down"]
    directions: list[Literal["maximize", "minimize"]] = ["maximize", "minimize"]
    weights = {"up": 1.0, "down": -1.0}

    def evaluate(self, strategy, dataset) -> TrialOutcome:
        p = strategy.params
        up, down = float(p["a"]), (p["b"] * 10.0) ** 2
        return TrialOutcome(params=dict(p), score=up - down, metrics={"up": up, "down": down})

    def score(self, strategy, dataset) -> float:
        return self.evaluate(strategy, dataset).score


def test_nsga2_searches_the_components_and_picks_from_the_pareto_front():
    result = _tune(
        OptunaTuner(seed=0, parallel=_SERIAL, sampler="nsga2", batch_size=6),
        _MultiObjective(),
        budget=24,
    )
    trials = [t for t in result.trials or [] if t.status == "ok"]
    assert len(result.trials or []) == 24
    assert all(t.metrics is not None for t in trials)

    def dominated(t: TrialOutcome) -> bool:
        m = t.metrics or {}
        return any(
            (o.metrics or {})["up"] >= m["up"]
            and (o.metrics or {})["down"] <= m["down"]
            and (o.metrics or {}) != m
            for o in trials
        )

    front = [t for t in trials if not dominated(t)]
    # the winner sits on the front and is its best weighted score
    assert result.best_score == max(t.score for t in front)
    assert any(dict(t.params) == dict(result.best_params) for t in front)


def test_nsga2_without_components_runs_one_objective():
    result = _tune(OptunaTuner(seed=0, parallel=_SERIAL, sampler="nsga2"), budget=8)
    assert len(result.history) == 8


def test_bad_settings_are_refused():
    with pytest.raises(ValueError):
        OptunaTuner(batch_size=0)
    with pytest.raises(ValueError):
        OptunaTuner(sampler="bogus")  # type: ignore[arg-type]


def test_seed_is_exposed_for_the_manifest():
    assert OptunaTuner(seed=11).seed == 11


# ---- pruning on the vectorised fast path ------------------------------------


class _Lookback:
    name = "lookback"
    direction: Literal["maximize", "minimize"] = "maximize"
    calls = 0

    def score(self, strategy, dataset) -> float:
        type(self).calls += 1
        return float(strategy.params["lookback_days"])


class _TwoAxis(Momentum):
    @classmethod
    def parameter_spec(cls):
        spec = {s.name: s for s in super().parameter_spec()}
        spec["lookback_days"] = ParameterSpec(
            name="lookback_days", kind="int", default=20, bounds=(10, 60)
        )
        return list(spec.values())


@pytest.fixture
def lake(tmp_path):
    lake = random_walk_lake(tmp_path / "lake.duckdb", ["A.US", "B.US", "C.US"], periods=200)
    yield lake
    lake.close()


def _dataset(lake) -> Any:
    from tests.fixtures.parallel_lab import lake_dates

    start, end = lake_dates(lake)
    return LabDataset(lake=lake, universe=["A.US", "B.US", "C.US"], start=start, end=end)


def test_pruning_skips_full_backtests_but_counts_every_pruned_trial(lake):
    _Lookback.calls = 0
    tuner = OptunaTuner(seed=0, parallel=_SERIAL, prune=True, batch_size=4, prune_startup=4)
    result = _tune(tuner, _Lookback(), 20, _TwoAxis, _dataset(lake))
    trials = result.trials or []
    pruned = [t for t in trials if t.status == "failed" and (t.error or "").startswith(PRUNED)]
    ok = [t for t in trials if t.status == "ok"]
    assert len(trials) == 20
    assert pruned, "the median pruner should stop some trials on the fast score"
    assert len(ok) == _Lookback.calls == 20 - len(pruned)
    assert all(math.isnan(t.score) for t in pruned)
    # the winner comes from full backtests only
    assert result.best_score == max(t.score for t in ok)


def test_pruning_is_off_for_a_strategy_without_the_fast_path():
    tuner = OptunaTuner(seed=0, parallel=_SERIAL, prune=True, prune_startup=2, batch_size=3)
    result = _tune(tuner, budget=9)
    assert all(t.status == "ok" for t in result.trials or [])
