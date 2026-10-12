"""The tuner registry: one decorated class with an ``Options`` model is a
tuner every transport accepts, and the old flat fields still work."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from stonks.app.lab import LabRunOptions, build_tuner
from stonks.lab.parallel import ParallelSettings
from stonks.lab.tuning import registry
from stonks.lab.tuning.grid import GridTuner
from stonks.lab.tuning.optuna import OptunaTuner
from stonks.lab.tuning.random import RandomTuner
from stonks.lab.tuning.registry import register_tuner, tuner_label, tuner_names
from stonks.lab.vectorized import PrescreenTuner

_ONE = ParallelSettings(max_workers=1)


@pytest.fixture
def scratch_registry(monkeypatch):
    monkeypatch.setattr(registry, "_TUNERS", dict(registry.tuner_classes()))


def test_the_built_in_tuners_are_registered():
    assert {"grid", "random", "optuna", "prescreen"} <= set(tuner_names())


@pytest.mark.parametrize(
    ("name", "cls"),
    [
        ("grid", GridTuner),
        ("random", RandomTuner),
        ("optuna", OptunaTuner),
        ("prescreen", PrescreenTuner),
    ],
)
def test_build_tuner_looks_the_class_up(name, cls):
    tuner = build_tuner(LabRunOptions(tuner=name, seed=3), _ONE)
    assert isinstance(tuner, cls)


def test_prescreen_is_selectable_with_options():
    options = LabRunOptions(tuner="prescreen", tuner_options={"grid_size": 4, "cost_bps": 1.0})
    tuner = build_tuner(options, _ONE)
    assert isinstance(tuner, PrescreenTuner)
    assert tuner._grid_size == 4
    assert tuner._cost_bps == 1.0


def test_tuner_options_are_validated_by_the_tuners_model():
    with pytest.raises(ValidationError, match="sampler"):
        LabRunOptions(tuner="optuna", tuner_options={"sampler": "annealing"})
    with pytest.raises(ValidationError, match="nope"):
        LabRunOptions(tuner="grid", tuner_options={"nope": 1})
    with pytest.raises(ValidationError, match="unknown tuner"):
        LabRunOptions(tuner="simulated_annealing")


def test_the_old_flat_fields_still_validate():
    options = LabRunOptions(tuner="optuna", seed=7, sampler="nsga2", prune=True)
    tuner = build_tuner(options, _ONE)
    assert isinstance(tuner, OptunaTuner)
    assert tuner.seed == 7
    assert tuner._sampler == "nsga2"
    assert tuner._prune is True
    grid = build_tuner(LabRunOptions(tuner="grid", grid_size=3), _ONE)
    assert isinstance(grid, GridTuner)
    assert grid._grid_size == 3


def test_tuner_options_win_over_the_flat_fields():
    options = LabRunOptions(tuner="grid", grid_size=3, tuner_options={"grid_size": 6})
    tuner = build_tuner(options, _ONE)
    assert isinstance(tuner, GridTuner)
    assert tuner._grid_size == 6


def test_flat_fields_for_another_tuner_are_ignored():
    # the CLI sends a sampler whatever the tuner; grid has no sampler
    tuner = build_tuner(LabRunOptions(tuner="grid", sampler="tpe", prune=False), _ONE)
    assert isinstance(tuner, GridTuner)
    assert tuner._grid_size == 5


def test_a_registered_tuner_is_accepted_with_no_other_edit(scratch_registry):
    @register_tuner("constant_test")
    class ConstantTuner(RandomTuner):
        """Constant.

        A test tuner."""

        class Options(BaseModel):
            model_config = ConfigDict(extra="forbid")
            level: int = 1

        def __init__(self, seed=0, parallel=None, level=1):
            super().__init__(seed=seed, parallel=parallel)
            self.level = level

    options = LabRunOptions(tuner="constant_test", tuner_options={"level": 4})
    tuner = build_tuner(options, _ONE)
    assert isinstance(tuner, ConstantTuner)
    assert tuner.level == 4
    assert tuner_label("constant_test") == "Constant"


def test_labels_come_from_the_docstring():
    assert tuner_label("grid") == "Grid search"
    assert tuner_label("prescreen")


def test_the_schema_lists_the_registry_names():
    prop = LabRunOptions.model_json_schema()["properties"]["tuner"]
    assert set(prop["enum"]) == set(tuner_names())
