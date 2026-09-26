"""API / MCP response models turn non-finite floats into null, so strict JSON
(``allow_nan=False``) never fails and stored job results re-validate."""

from __future__ import annotations

import json
import math
from datetime import date

import pytest

from stonks.app.lab import BacktestResult, BenchmarkStatsView, LabRunView, TradeStatsView
from stonks.app.serialize import FiniteFloat
from stonks.app.strategies import SurvivalReportView

INF, NAN = float("inf"), float("nan")


def _strict(model) -> dict:
    doc = model.model_dump(mode="json")
    json.dumps(doc, allow_nan=False)  # raises on NaN / inf
    return doc


def _bench(**overrides) -> BenchmarkStatsView:
    base = dict.fromkeys(
        (
            "benchmark_cagr",
            "excess_cagr",
            "benchmark_sharpe",
            "benchmark_max_dd",
            "beta",
            "alpha_annual",
            "alpha_tstat",
            "r2",
            "residual_sharpe",
            "tracking_error",
            "information_ratio",
            "up_capture",
            "down_capture",
            "correlation",
        ),
        0.1,
    )
    base.update(overrides)
    return BenchmarkStatsView(name="EW", spec="auto", n_obs=10, **base)


def test_finite_float_maps_nan_and_inf_to_none_and_keeps_the_rest():
    from pydantic import TypeAdapter

    ta = TypeAdapter(FiniteFloat)
    assert ta.validate_python(INF) is None
    assert ta.validate_python(-INF) is None
    assert ta.validate_python(NAN) is None
    assert ta.validate_python(1.5) == 1.5
    assert ta.validate_python(None) is None
    with pytest.raises(ValueError):
        ta.validate_python("abc")


def test_benchmark_stats_view_nulls_non_finite_stats():
    doc = _strict(_bench(alpha_tstat=INF, excess_cagr=NAN, information_ratio=-INF))
    assert doc["alpha_tstat"] is None
    assert doc["excess_cagr"] is None
    assert doc["information_ratio"] is None
    assert doc["beta"] == 0.1


def test_backtest_result_nulls_non_finite_fields():
    stats = TradeStatsView(
        n_trades=1,
        n_open=0,
        win_rate=NAN,
        avg_win=INF,
        avg_loss=0.0,
        payoff_ratio=INF,
        expectancy=NAN,
        trade_profit_factor=INF,
        avg_bars_held=1.0,
        exposure=1.0,
        turnover_annual=INF,
        costs_paid=0.0,
        cost_drag_annual=NAN,
    )
    result = BacktestResult(
        strategy_id="s",
        interval="1d",
        start=date(2025, 1, 1),
        end=date(2025, 2, 1),
        final_return=INF,
        sharpe=NAN,
        max_drawdown=0.0,
        cagr=INF,
        profit_factor=INF,
        equity=[],
        trade_stats=stats,
        sortino=INF,
        benchmark=_bench(alpha_tstat=INF),
    )
    doc = _strict(result)
    assert doc["sharpe"] is None and doc["cagr"] is None and doc["sortino"] is None
    assert doc["trade_stats"]["win_rate"] is None
    assert doc["trade_stats"]["turnover_annual"] is None
    assert doc["benchmark"]["alpha_tstat"] is None
    # a stored job result (nulls) validates back into the model
    assert BacktestResult.model_validate(doc).trade_stats.win_rate is None


def test_golive_views_null_non_finite_values():
    from stonks.app.golive import GoLiveCheckView, PromotionChecklistView

    check = GoLiveCheckView(name="min_days", passed=False, value=3.0, limit=INF, detail="")
    assert _strict(check)["limit"] is None
    checklist = PromotionChecklistView(dsr=NAN, pbo=0.1, excess_cagr=-INF)
    doc = _strict(checklist)
    assert doc["dsr"] is None and doc["excess_cagr"] is None and doc["pbo"] == 0.1


def test_lab_run_view_nulls_non_finite_metrics_and_benchmark():
    view = LabRunView(
        class_path="m:C",
        best_params={},
        best_score=INF,
        verdict="fail",
        survival_reports=[
            SurvivalReportView(test_id="mc_trades", passed=False, metrics={"x": INF}, notes="")
        ],
        registered_strategy_id=None,
        benchmark=_bench(excess_cagr=INF),
    )
    doc = _strict(view)
    assert doc["best_score"] is None
    assert doc["survival_reports"][0]["metrics"]["x"] is None
    assert doc["benchmark"]["excess_cagr"] is None
    assert not math.isnan(LabRunView.model_validate(doc).benchmark.beta)
