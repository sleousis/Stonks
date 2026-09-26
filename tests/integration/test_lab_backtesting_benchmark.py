"""Every lab backtest carries its benchmark (BL-22)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.benchmark import AUTO_BENCHMARK_TICKER, BenchmarkedReport
from stonks.core.interval import Interval
from stonks.lab.backtesting import run_backtest, run_backtest_with_fills
from stonks.lab.dataset import LabDataset
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.buy_and_hold import BuyAndHold

DAYS = [d.date() for d in pd.bdate_range("2024-01-02", periods=60)]


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
    rng = np.random.default_rng(7)
    _upsert(lake, "A.US", 100 * np.cumprod(1 + rng.normal(0.001, 0.01, len(DAYS))))
    _upsert(lake, "B.US", 50 * np.cumprod(1 + rng.normal(0.0, 0.02, len(DAYS))))
    yield lake
    lake.close()


def _dataset(lake, **extra) -> LabDataset:
    ds = LabDataset(
        lake=lake,
        universe=["A.US", "B.US"],
        start=DAYS[0],
        end=DAYS[-1],
        interval=Interval.DAY_1,
    )
    for k, v in extra.items():
        setattr(ds, k, v)  # duck-typed, as LabDataset.benchmark lands elsewhere
    return ds


def test_default_is_auto_and_falls_back_to_equal_weight(lake):
    report = run_backtest(BuyAndHold({"ticker": "A.US"}), _dataset(lake), (DAYS[0], DAYS[-1]))
    assert isinstance(report, BenchmarkedReport)
    bench = report.benchmark
    assert bench is not None
    assert bench.curve.name == "EW"
    assert bench.curve.members == ("A.US", "B.US")
    assert list(bench.curve.dates) == list(report.equity_dates)
    assert bench.curve.values[0] == pytest.approx(report.equity_curve[0])
    assert bench.stats.n_obs == len(report.equity_curve) - 1


def test_trade_ledger_is_still_attached(lake):
    report, fills = run_backtest_with_fills(
        BuyAndHold({"ticker": "A.US"}), _dataset(lake), (DAYS[0], DAYS[-1])
    )
    assert fills and report.trades
    assert report.benchmark is not None


def test_holding_the_benchmark_gives_beta_one(lake):
    report = run_backtest(
        BuyAndHold({"ticker": "A.US"}), _dataset(lake, benchmark="A.US"), (DAYS[0], DAYS[-1])
    )
    stats = report.benchmark.stats
    assert report.benchmark.curve.name == "A.US"
    # fully invested from the second bar: beta ~1, near-zero tracking
    assert stats.beta == pytest.approx(1.0, abs=0.05)
    assert stats.correlation > 0.95


def test_auto_uses_the_market_ticker_when_present(lake):
    _upsert(lake, AUTO_BENCHMARK_TICKER, np.linspace(400, 420, len(DAYS)))
    report = run_backtest(BuyAndHold({"ticker": "A.US"}), _dataset(lake), (DAYS[0], DAYS[-1]))
    assert report.benchmark.curve.name == AUTO_BENCHMARK_TICKER


def test_none_disables_the_benchmark(lake):
    report = run_backtest(
        BuyAndHold({"ticker": "A.US"}), _dataset(lake, benchmark="none"), (DAYS[0], DAYS[-1])
    )
    assert report.benchmark is None


def test_explicit_argument_overrides_the_dataset(lake):
    ds = _dataset(lake, benchmark="none")
    report = run_backtest(BuyAndHold({"ticker": "A.US"}), ds, (DAYS[0], DAYS[-1]), benchmark="B.US")
    assert report.benchmark.curve.name == "B.US"


def test_empty_window_has_no_benchmark(lake):
    ds = _dataset(lake)
    ds.universe = ["NOBARS.US"]
    report = run_backtest(BuyAndHold({"ticker": "NOBARS.US"}), ds, (DAYS[0], DAYS[-1]))
    assert report.benchmark is None
