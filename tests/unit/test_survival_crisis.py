"""Crisis-window survival test (BL-48)."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stonks.core.types import Order
from stonks.lab.dataset import LabDataset
from stonks.lab.survival.crisis import CRISIS_WINDOWS
from stonks.lab.survival.registry import build_survival_test, resolve_preset, survival_test_names
from stonks.strategies.base import BaseStrategy
from tests.unit.nt888_helpers import write_bars

DATES = pd.bdate_range("2019-10-01", "2020-06-30")
CRASH = (DATES >= "2020-02-19") & (DATES <= "2020-03-23")


class HoldAll(BaseStrategy):
    id = "hold_all"
    applicable_asset_classes = ("equity",)

    def estimate_return(self, ticker, as_of, lake):
        return 1.0

    def decide(self, my_picks, portfolio, prices, as_of):
        return [
            Order(
                client_id=f"b:{t}:{as_of}",
                ticker=t,
                side="buy",
                quantity=0.99 * portfolio.cash / prices[t],
            )
            for _, t in my_picks
            if portfolio.positions.get(t, 0.0) <= 0 and prices.get(t)
        ]


def _path(crash_total: float) -> np.ndarray:
    """Flat, then a steady fall of ``crash_total`` inside the COVID window."""
    steps = np.where(CRASH, np.log(1 - crash_total) / CRASH.sum(), 0.0)
    return 100 * np.exp(np.cumsum(steps))


@pytest.fixture
def crisis_lake(lake):
    write_bars(lake, "SPY.US", DATES, _path(0.30), spread=0.0)
    write_bars(lake, "MILD.US", DATES, _path(0.20), spread=0.0)
    write_bars(lake, "WILD.US", DATES, _path(0.70), spread=0.0)
    return lake


def _ds(lake, ticker):
    return LabDataset(
        lake=lake,
        universe=[ticker],
        start=DATES[0].date(),
        end=DATES[-1].date(),
        benchmark="SPY.US",
    )


def test_registered_and_in_the_promotion_preset():
    assert "crisis" in survival_test_names()
    assert "crisis" in resolve_preset("promotion")
    assert {w.name for w in CRISIS_WINDOWS} == {
        "gfc",
        "euro_2011",
        "q4_2018",
        "covid",
        "h1_2022",
        "crypto_2022",
    }


def test_milder_drawdown_than_the_benchmark_passes(crisis_lake):
    report = build_survival_test("crisis").run(HoldAll({}), _ds(crisis_lake, "MILD.US"))
    assert report.passed, report.notes
    m = report.metrics
    assert m["n_covered"] == 1 and m["crisis_coverage"] == pytest.approx(1 / 6)
    assert m["bench_dd_covid"] == pytest.approx(-0.30, abs=0.02)
    assert m["dd_covid"] > -0.25


def test_a_synthetic_crash_fails(crisis_lake):
    report = build_survival_test("crisis").run(HoldAll({}), _ds(crisis_lake, "WILD.US"))
    assert not report.passed
    assert "covid" in report.notes


def test_windows_without_data_are_skipped(crisis_lake):
    ds = LabDataset(
        lake=crisis_lake,
        universe=["WILD.US"],
        start=date(2020, 4, 1),
        end=date(2020, 6, 30),
        benchmark="SPY.US",
    )
    report = build_survival_test("crisis").run(HoldAll({}), ds)
    assert report.passed and report.metrics["crisis_coverage"] == 0.0
    assert "no crisis window" in report.notes
    strict = build_survival_test("crisis", {"require_coverage": True}).run(HoldAll({}), ds)
    assert not strict.passed


def test_custom_windows_and_ratio(crisis_lake):
    options = {
        "windows": [{"name": "spring", "start": "2020-02-19", "end": "2020-03-23"}],
        "max_dd_ratio": 3.0,
    }
    report = build_survival_test("crisis", options).run(HoldAll({}), _ds(crisis_lake, "WILD.US"))
    assert report.passed, report.notes
    assert report.metrics["n_windows"] == 1 and "dd_spring" in report.metrics


# ---- a missing benchmark and in-sample windows (BE-33) ---------------------------------


def test_a_covered_crisis_with_no_benchmark_bars_does_not_pass(crisis_lake):
    ds = LabDataset(
        lake=crisis_lake,
        universe=["MILD.US"],
        start=DATES[0].date(),
        end=DATES[-1].date(),
        benchmark="NOPE.US",
    )
    report = build_survival_test("crisis").run(HoldAll({}), ds)
    assert not report.passed
    assert "benchmark missing" in report.notes


def test_windows_the_tuner_saw_are_reported_as_in_sample(crisis_lake):
    tuned_on_it = _ds(crisis_lake, "MILD.US")  # default split: COVID is in training
    report = build_survival_test("crisis").run(HoldAll({}), tuned_on_it)
    assert report.metrics["in_sample_covid"] == 1.0
    assert report.metrics["n_covered_oos"] == 0.0
    assert "in sample" in report.notes
    held_out = LabDataset(
        lake=crisis_lake,
        universe=["MILD.US"],
        start=DATES[0].date(),
        end=DATES[-1].date(),
        train_end=date(2020, 1, 31),
        benchmark="SPY.US",
    )
    report = build_survival_test("crisis").run(HoldAll({}), held_out)
    assert report.metrics["in_sample_covid"] == 0.0
    assert report.metrics["n_covered_oos"] == 1.0
