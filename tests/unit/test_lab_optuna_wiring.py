"""The Optuna tuner and the risk-aware objectives are choices in the API,
the CLI and MCP (22.1)."""

from __future__ import annotations

from typing import get_args

from stonks.app.lab import (
    _OBJECTIVES,
    LabRunOptions,
    ObjectiveName,
    TunerName,
    _CancellableObjective,
    build_tuner,
)
from stonks.lab.objectives import MultiMetricObjective
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
        assert name in get_args(ObjectiveName)
        assert _OBJECTIVES[name]().name == name
        assert LabRunOptions(objective=name).objective == name


def test_cli_and_mcp_offer_the_same_tuners_and_samplers():
    from stonks.cli import _LAB_SAMPLERS, _LAB_TUNERS
    from stonks.lab.tuning.optuna import SamplerName
    from stonks.mcp.tools.common import SamplerName as McpSamplerName
    from stonks.mcp.tools.common import TunerName as McpTunerName

    assert set(_LAB_TUNERS) == set(get_args(TunerName)) == set(get_args(McpTunerName))
    assert set(_LAB_SAMPLERS) == set(get_args(SamplerName)) == set(get_args(McpSamplerName))


class _Ctx:
    def check_cancelled(self) -> None:
        return None


def test_the_cancellable_wrapper_keeps_the_parts_a_pareto_search_reads():
    inner = MultiMetricObjective()
    wrapped = _CancellableObjective(inner, _Ctx())  # type: ignore[arg-type]
    assert wrapped.metric_names == inner.metric_names  # type: ignore[attr-defined]
    assert wrapped.directions == inner.directions  # type: ignore[attr-defined]
