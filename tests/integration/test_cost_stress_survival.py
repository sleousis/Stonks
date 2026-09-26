"""CostStressTest on real backtests (BL-18)."""

from __future__ import annotations

import dataclasses

import pytest

from stonks.backtest.costs import AssetClassCosts, CostModelSettings
from stonks.lab.survival.cost_stress import CostStressTest
from tests.fixtures.robustness_lab import FlipFlop, LongAll, dataset_for, trend_lake

_COSTS = CostModelSettings(default=AssetClassCosts(half_spread_bps=5.0, fee_bps=1.0))


@pytest.fixture
def lake():
    lake = trend_lake(":memory:", {"A.US": (0.003, 0.008), "B.US": (0.002, 0.01)})
    yield lake
    lake.close()


def test_buy_and_hold_passes(lake):
    ds = dataset_for(lake, ["A.US", "B.US"], costs=_COSTS)
    report = CostStressTest(max_workers=1).run(LongAll({}), ds)
    assert report.passed, report.notes
    m = report.metrics
    for key in ("sharpe_0x", "sharpe_1x", "sharpe_2x", "sharpe_3x"):
        assert key in m
    assert m["sharpe_0x"] >= m["sharpe_1x"] >= m["sharpe_2x"] >= m["sharpe_3x"] > 0
    assert m["break_even_multiple"] == 10.0
    assert m["cost_sr"] <= m["cost_sr_limit"]
    assert m["used_realistic_costs"] == 0.0


def test_high_turnover_fails_the_speed_limit(lake):
    ds = dataset_for(lake, ["A.US", "B.US"], costs=_COSTS)
    report = CostStressTest(max_workers=1).run(FlipFlop({"hold_bars": 1}), ds)
    assert not report.passed
    assert report.metrics["cost_sr"] > report.metrics["cost_sr_limit"]
    assert "speed limit" in report.notes


def test_break_even_multiple_falls_as_base_costs_rise(lake):
    strategy = FlipFlop({"hold_bars": 8})
    base = AssetClassCosts(half_spread_bps=10.0, fee_bps=2.0)
    doubled = AssetClassCosts(half_spread_bps=20.0, fee_bps=4.0)
    cheap = dataset_for(lake, ["A.US"], costs=CostModelSettings(default=base))
    dear = dataclasses.replace(cheap, costs=CostModelSettings(default=doubled))
    be_cheap = CostStressTest(max_workers=1).run(strategy, cheap).metrics["break_even_multiple"]
    be_dear = CostStressTest(max_workers=1).run(strategy, dear).metrics["break_even_multiple"]
    assert 0.0 < be_dear < be_cheap < 10.0
    # doubling the base costs halves the break-even multiple
    assert be_dear == pytest.approx(be_cheap / 2, abs=0.3)


def test_no_costs_on_the_dataset_uses_realistic_costs(lake):
    ds = dataset_for(lake, ["A.US", "B.US"])
    report = CostStressTest(max_workers=1).run(LongAll({}), ds)
    assert report.metrics["used_realistic_costs"] == 1.0
    assert "realistic" in report.notes
    assert report.metrics["sharpe_1x"] < report.metrics["sharpe_0x"]


def test_no_trades_fails_for_lack_of_data(lake):
    ds = dataset_for(lake, ["A.US"], costs=_COSTS)
    report = CostStressTest(max_workers=1).run(LongAll({"ticker": "NOPE.US"}), ds)
    assert not report.passed
    assert "insufficient data" in report.notes


def test_result_does_not_depend_on_worker_count(tmp_path):
    lake = trend_lake(tmp_path / "lake.duckdb", {"A.US": (0.003, 0.008), "B.US": (0.002, 0.01)})
    try:
        ds = dataset_for(lake, ["A.US", "B.US"], costs=_COSTS)
        serial = CostStressTest(max_workers=1).run(FlipFlop({"hold_bars": 6}), ds)
        pooled = CostStressTest(max_workers=2).run(FlipFlop({"hold_bars": 6}), ds)
    finally:
        lake.close()
    assert serial == pooled
