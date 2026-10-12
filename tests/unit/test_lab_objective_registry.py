"""The objective registry: one decorated class is an objective every
transport accepts, with its ``cv_`` variant when it asks for one."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from stonks.app.lab import LabRunOptions
from stonks.lab import objectives
from stonks.lab.cv import CVObjective
from stonks.lab.objectives import (
    SharpeObjective,
    build_objective,
    describe_objective,
    objective_label,
    objective_names,
    register_objective,
)


@pytest.fixture
def scratch_registry(monkeypatch):
    """Registrations made in a test vanish after it."""
    monkeypatch.setattr(objectives, "_PLAIN", dict(objectives._PLAIN))
    monkeypatch.setattr(objectives, "_CV", list(objectives._CV))


def test_the_built_in_names_keep_their_cv_variants():
    names = objective_names()
    for name in ("sharpe", "cagr", "final_return", "sortino", "calmar", "sharpe_dd", "multi"):
        assert name in names
    for name in ("cv_sharpe", "cv_cagr", "cv_final_return"):
        assert name in names
    # only the three historic objectives have a cross-validated variant
    assert "cv_sortino" not in names
    assert "cv_multi" not in names


def test_a_registered_objective_is_accepted_with_its_cv_variant(scratch_registry):
    @register_objective("omega_test", cv=True)
    class OmegaObjective(SharpeObjective):
        """Omega.

        A test objective."""

    assert LabRunOptions(objective="omega_test").objective == "omega_test"
    assert LabRunOptions(objective="cv_omega_test").objective == "cv_omega_test"
    built = build_objective("cv_omega_test")
    assert isinstance(built, CVObjective)
    assert isinstance(built.inner, OmegaObjective)
    assert built.name == "cv_omega_test"
    assert build_objective("omega_test").name == "omega_test"


def test_an_unknown_objective_is_refused():
    with pytest.raises(ValidationError, match="unknown objective"):
        LabRunOptions(objective="no_such_objective")
    with pytest.raises(ValueError, match="unknown objective"):
        build_objective("no_such_objective")


def test_two_classes_cannot_share_a_name(scratch_registry):
    with pytest.raises(ValueError, match="already registered"):

        @register_objective("sharpe")
        class Other(SharpeObjective):
            pass


def test_labels_come_from_the_docstring():
    assert objective_label("sharpe") == "Sharpe"
    assert objective_label("cv_sharpe") == "Sharpe on purged folds"
    assert objective_label("final_return") == "Total return"
    assert describe_objective("sharpe_dd")
    assert "purged folds" in describe_objective("cv_cagr")


def test_the_schema_lists_the_registry_names():
    schema = LabRunOptions.model_json_schema()
    prop = schema["properties"]["objective"]
    assert set(prop["enum"]) == set(objective_names())
