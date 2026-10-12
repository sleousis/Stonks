"""The Optuna tuner and the risk-aware objectives are choices in the API,
the CLI and MCP (22.1)."""

from __future__ import annotations

import pytest
import typer
from pydantic import TypeAdapter

from stonks.app.lab import (
    LabRunOptions,
    _CancellableObjective,
    build_tuner,
    lab_objectives,
)
from stonks.lab.objectives import MultiMetricObjective, objective_names
from stonks.lab.parallel import ParallelSettings
from stonks.lab.tuning.optuna import OptunaTuner


def test_build_tuner_makes_the_optuna_tuner_from_the_options():
    options = LabRunOptions(tuner="optuna", seed=7, sampler="nsga2", prune=True)
    tuner = build_tuner(options, ParallelSettings(max_workers=1))
    assert isinstance(tuner, OptunaTuner)
    assert tuner.seed == 7
    assert tuner._sampler == "nsga2"
    assert tuner._prune is True


def test_the_new_objectives_are_choices():
    for name in ("sortino", "calmar", "sharpe_dd", "multi"):
        assert name in objective_names()
        assert lab_objectives()[name]().name == name
        assert LabRunOptions(objective=name).objective == name


def test_cli_and_mcp_accept_every_registry_tuner():
    from stonks.cli import _tuner_choice
    from stonks.lab.tuning.registry import tuner_names
    from stonks.mcp.tools.common import TunerName as McpTunerName

    adapter = TypeAdapter(McpTunerName)
    for name in tuner_names():
        assert _tuner_choice(name) == name
        assert adapter.validate_python(name) == name
    with pytest.raises(typer.BadParameter, match="--tuner"):
        _tuner_choice("no_such_tuner")


class _Ctx:
    def check_cancelled(self) -> None:
        return None


def test_the_cancellable_wrapper_keeps_the_parts_a_pareto_search_reads():
    inner = MultiMetricObjective()
    wrapped = _CancellableObjective(inner, _Ctx())  # type: ignore[arg-type]
    assert wrapped.metric_names == inner.metric_names  # type: ignore[attr-defined]
    assert wrapped.directions == inner.directions  # type: ignore[attr-defined]
