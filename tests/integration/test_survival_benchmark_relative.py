"""``benchmark_relative`` survival test (BL-22, BL-23)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.lab.dataset import LabDataset
from stonks.lab.survival.benchmark_relative import BenchmarkRelativeTest
from stonks.lab.survival.registry import build_survival_test, survival_test_names
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.buy_and_hold import BuyAndHold

DAYS = [d.date() for d in pd.bdate_range("2023-01-02", periods=200)]


def _upsert(lake, ticker, closes):
    closes = np.asarray(closes, dtype=float)
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": ticker,
                "date": DAYS[: len(closes)],
                "open": closes,
                "high": closes,
                "low": closes,
                "close": closes,
                "adj_close": closes,
                "volume": 1_000_000.0,
            }
        )
    )


@pytest.fixture
def lake(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    rng = np.random.default_rng(11)
    market = rng.normal(0.0, 0.01, len(DAYS))
    # WIN drifts up on top of the common factor, LOSE drifts down
    _upsert(lake, "WIN.US", 100 * np.cumprod(1 + market + 0.002 + rng.normal(0, 0.003, len(DAYS))))
    _upsert(lake, "LOSE.US", 100 * np.cumprod(1 + market - 0.002 + rng.normal(0, 0.003, len(DAYS))))
    yield lake
    lake.close()


def _dataset(lake, **extra):
    ds = LabDataset(
        lake=lake,
        universe=["WIN.US", "LOSE.US"],
        start=DAYS[0],
        end=DAYS[-1],
        interval=Interval.DAY_1,
    )
    for k, v in extra.items():
        setattr(ds, k, v)
    return ds


def test_registered_under_its_id():
    assert "benchmark_relative" in survival_test_names()
    test = build_survival_test("benchmark_relative", {"min_ir": 0.5})
    assert isinstance(test, BenchmarkRelativeTest)


def test_outperformer_passes_and_reports_attribution(lake):
    report = BenchmarkRelativeTest().run(BuyAndHold({"ticker": "WIN.US"}), _dataset(lake))
    assert report.test_id == "benchmark_relative"
    assert report.passed
    m = report.metrics
    assert m["information_ratio"] > 0
    assert m["excess_cagr"] > 0
    for key in ("beta", "alpha_tstat", "alpha_annual", "tracking_error", "benchmark_cagr"):
        assert key in m and isinstance(m[key], float)
    assert "EW" in report.notes


def test_underperformer_fails_but_still_reports_beta(lake):
    report = BenchmarkRelativeTest().run(BuyAndHold({"ticker": "LOSE.US"}), _dataset(lake))
    assert not report.passed
    assert report.metrics["information_ratio"] < 0
    assert "beta" in report.metrics and "alpha_tstat" in report.metrics


def test_alpha_tstat_requirement(lake):
    strict = BenchmarkRelativeTest(require_alpha_tstat=1_000.0)
    report = strict.run(BuyAndHold({"ticker": "WIN.US"}), _dataset(lake))
    assert not report.passed


def test_uses_the_validation_window_by_default(lake):
    ds = _dataset(lake)
    val = BenchmarkRelativeTest().run(BuyAndHold({"ticker": "WIN.US"}), ds)
    full = BenchmarkRelativeTest(window="full").run(BuyAndHold({"ticker": "WIN.US"}), ds)
    n_val = sum(1 for d in DAYS if ds.val_window[0] <= d <= ds.val_window[1])
    assert val.metrics["n_obs"] == n_val - 1
    assert full.metrics["n_obs"] == len(DAYS) - 1


def test_rejects_unknown_window():
    with pytest.raises(ValueError):
        BenchmarkRelativeTest(window="train")


def test_named_benchmark_option_overrides_the_dataset(lake):
    report = BenchmarkRelativeTest(benchmark="LOSE.US").run(
        BuyAndHold({"ticker": "WIN.US"}), _dataset(lake, benchmark="WIN.US")
    )
    assert "LOSE.US" in report.notes
    assert report.passed


def test_dataset_benchmark_is_used(lake):
    report = BenchmarkRelativeTest().run(
        BuyAndHold({"ticker": "LOSE.US"}), _dataset(lake, benchmark="LOSE.US")
    )
    assert "LOSE.US" in report.notes


def test_disabled_dataset_benchmark_falls_back_to_auto(lake):
    report = BenchmarkRelativeTest().run(
        BuyAndHold({"ticker": "WIN.US"}), _dataset(lake, benchmark="none")
    )
    assert "EW" in report.notes
    assert report.passed


def test_unpriced_benchmark_fails_with_a_note(lake):
    report = BenchmarkRelativeTest(benchmark="NOPE.US").run(
        BuyAndHold({"ticker": "WIN.US"}), _dataset(lake)
    )
    assert not report.passed
    assert "NOPE.US" in report.notes


# ---- RS-09: no evidence is not a pass ------------------------------------------------


def test_do_nothing_strategy_against_a_falling_benchmark_fails(lake):
    _upsert(lake, "DOWN.US", 100 * np.cumprod(np.full(len(DAYS), 1 - 0.002)))
    idle = BuyAndHold({"ticker": "ABSENT.US"})  # never trades
    report = BenchmarkRelativeTest(benchmark="DOWN.US").run(idle, _dataset(lake))
    assert not report.passed
    assert "insufficient data" in report.notes


def test_zero_tracking_error_fails(lake):
    _upsert(lake, "FLAT.US", np.full(len(DAYS), 100.0))
    idle = BuyAndHold({"ticker": "ABSENT.US"})
    report = BenchmarkRelativeTest(benchmark="FLAT.US").run(idle, _dataset(lake))
    assert report.metrics["tracking_error"] == 0.0
    assert not report.passed


def test_a_few_bars_are_not_enough_evidence(lake):
    short = LabDataset(
        lake=lake,
        universe=["WIN.US", "LOSE.US"],
        start=DAYS[0],
        end=DAYS[5],
        interval=Interval.DAY_1,
    )
    report = BenchmarkRelativeTest(benchmark="LOSE.US", window="full").run(
        BuyAndHold({"ticker": "WIN.US"}), short
    )
    assert not report.passed
    assert "insufficient data" in report.notes
