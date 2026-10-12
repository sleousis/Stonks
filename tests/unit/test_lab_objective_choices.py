"""The cross-validated objectives (``lab.cv.CVObjective``) are choices in
the API, the CLI and MCP, next to the plain ones, and every transport reads
the names from the objective registry."""

from __future__ import annotations

import pytest
import typer
from pydantic import TypeAdapter

from stonks.app.lab import LabRunOptions, lab_objectives
from stonks.lab.cv import CVObjective
from stonks.lab.objectives import (
    CAGRObjective,
    FinalReturnObjective,
    SharpeObjective,
    objective_names,
)


@pytest.mark.parametrize(
    ("name", "inner"),
    [
        ("cv_sharpe", SharpeObjective),
        ("cv_cagr", CAGRObjective),
        ("cv_final_return", FinalReturnObjective),
    ],
)
def test_cv_objectives_wrap_the_plain_ones(name, inner):
    objective = lab_objectives()[name]()
    assert isinstance(objective, CVObjective)
    assert isinstance(objective.inner, inner)
    assert objective.name == name
    assert LabRunOptions(objective=name).objective == name


def test_every_objective_name_has_a_builder():
    assert set(objective_names()) == set(lab_objectives())


def test_the_cli_accepts_every_registry_objective():
    from stonks.cli import _objective_choice

    for name in objective_names():
        assert _objective_choice(name) == name
    with pytest.raises(typer.BadParameter, match="--objective"):
        _objective_choice("no_such_objective")


def test_mcp_takes_any_objective_name_and_points_at_the_list():
    from stonks.mcp.tools.common import ObjectiveName as McpObjectiveName

    adapter = TypeAdapter(McpObjectiveName)
    for name in objective_names():
        assert adapter.validate_python(name) == name
    assert "list_lab_objectives" in str(adapter.json_schema())
