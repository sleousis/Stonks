"""Risk-aware objectives (22.1): Sortino, Calmar, drawdown-penalised
Sharpe and the multi-metric objective, scored from a backtest report."""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pytest

from stonks.backtest.report import compute_report
from stonks.lab.objectives import (
    OBJECTIVES,
    RATIO_CAP,
    CalmarObjective,
    DrawdownSharpeObjective,
    MultiMetricObjective,
    SortinoObjective,
    metric_value,
)


def _report(curve: list[float]):
    start = date(2024, 1, 1)
    dates = [start + timedelta(days=i) for i in range(len(curve))]
    return compute_report("s", dates, curve)


def _walk(n: int = 300, seed: int = 3) -> list[float]:
    rng = np.random.default_rng(seed)
    return list(100.0 * np.cumprod(1.0 + rng.normal(0.0004, 0.01, n)))


_BUMPY = _walk()
_SMOOTH = [100.0 + i for i in range(8)]  # never draws down


def test_sortino_and_calmar_read_the_report():
    report = _report(_BUMPY)
    assert SortinoObjective().metric(report) == pytest.approx(report.sortino)
    assert CalmarObjective().metric(report) == pytest.approx(report.calmar)
    assert SortinoObjective.name == "sortino"
    assert CalmarObjective.name == "calmar"


def test_ratios_without_downside_are_capped_so_the_trial_stays_scored():
    # No drawdown gives an infinite Calmar and Sortino; the ledger treats a
    # non-finite score as a failed trial, so the objectives cap it.
    report = _report(_SMOOTH)
    assert math.isinf(report.calmar)
    assert CalmarObjective().metric(report) == RATIO_CAP
    assert SortinoObjective().metric(report) == RATIO_CAP


def test_drawdown_sharpe_subtracts_the_drawdown_times_the_penalty():
    report = _report(_BUMPY)
    objective = DrawdownSharpeObjective(penalty=3.0)
    expected = report.sharpe - 3.0 * abs(report.max_drawdown)
    assert objective.metric(report) == pytest.approx(expected)
    assert objective.name == "sharpe_dd"
    with pytest.raises(ValueError):
        DrawdownSharpeObjective(penalty=-1.0)


def test_multi_metric_is_a_weighted_sum_and_keeps_each_component():
    report = _report(_BUMPY)
    objective = MultiMetricObjective({"sharpe": 1.0, "calmar": 0.5, "drawdown": -2.0})
    expected = report.sharpe + 0.5 * report.calmar - 2.0 * abs(report.max_drawdown)
    assert objective.metric(report) == pytest.approx(expected)
    assert objective.components(report) == pytest.approx(
        {"sharpe": report.sharpe, "calmar": report.calmar, "drawdown": abs(report.max_drawdown)}
    )
    # a negative weight means lower is better (the Pareto direction)
    assert objective.directions == ["maximize", "maximize", "minimize"]
    assert objective.metric_names == ["sharpe", "calmar", "drawdown"]


def test_multi_metric_rejects_unknown_or_empty_weights():
    with pytest.raises(ValueError, match="unknown metric"):
        MultiMetricObjective({"alpha": 1.0})
    with pytest.raises(ValueError, match="at least one"):
        MultiMetricObjective({})
    with pytest.raises(ValueError, match="non-zero"):
        MultiMetricObjective({"sharpe": 0.0})


def test_metric_value_knows_every_component():
    report = _report(_BUMPY)
    assert metric_value(report, "drawdown") == pytest.approx(abs(report.max_drawdown))
    assert metric_value(report, "ulcer_index") == pytest.approx(report.ulcer_index)
    with pytest.raises(ValueError):
        metric_value(report, "nope")


def test_registry_builds_every_objective_by_name():
    for name, factory in OBJECTIVES.items():
        assert factory().name == name
    assert {"sharpe", "sortino", "calmar", "sharpe_dd", "multi"} <= set(OBJECTIVES)


def test_evaluate_carries_the_components_to_the_trial(monkeypatch):
    report = _report(_BUMPY)
    monkeypatch.setattr("stonks.lab.objectives._backtest", lambda s, d, w: report)

    class _Dataset:
        train_window = (date(2024, 1, 1), date(2024, 12, 31))

    class _Strategy:
        params = {"a": 1}

    multi = MultiMetricObjective().evaluate(_Strategy(), _Dataset())  # type: ignore[arg-type]
    assert multi.metrics == MultiMetricObjective().components(report)
    assert multi.score == pytest.approx(MultiMetricObjective().metric(report))
    plain = SortinoObjective().evaluate(_Strategy(), _Dataset())  # type: ignore[arg-type]
    assert plain.metrics is None
