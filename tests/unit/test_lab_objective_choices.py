"""The cross-validated objectives (``lab.cv.CVObjective``) are choices in
the API, the CLI and MCP, next to the plain ones."""

from __future__ import annotations

from typing import get_args

import pytest

from stonks.app.lab import _OBJECTIVES, LabRunOptions, ObjectiveName
from stonks.lab.cv import CVObjective
from stonks.lab.objectives import CAGRObjective, FinalReturnObjective, SharpeObjective


@pytest.mark.parametrize(
    ("name", "inner"),
    [
        ("cv_sharpe", SharpeObjective),
        ("cv_cagr", CAGRObjective),
        ("cv_final_return", FinalReturnObjective),
    ],
)
def test_cv_objectives_wrap_the_plain_ones(name, inner):
    objective = _OBJECTIVES[name]()
    assert isinstance(objective, CVObjective)
    assert isinstance(objective.inner, inner)
    assert objective.name == name
    assert LabRunOptions(objective=name).objective == name


def test_every_objective_name_has_a_builder():
    assert set(get_args(ObjectiveName)) == set(_OBJECTIVES)


def test_cli_and_mcp_offer_the_same_objectives():
    from stonks.cli import _LAB_OBJECTIVES
    from stonks.mcp.tools.common import ObjectiveName as McpObjectiveName

    assert set(_LAB_OBJECTIVES) == set(get_args(ObjectiveName))
    assert set(get_args(McpObjectiveName)) == set(get_args(ObjectiveName))
