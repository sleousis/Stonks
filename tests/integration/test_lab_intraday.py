"""Lab objectives and survival tests must backtest at ``dataset.interval``.

Before the fix they built ``BacktestConfig`` without the interval, so an
intraday dataset was backtested against (non-existent) daily bars and got
an empty equity curve — every score collapsed to 0.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.lab.dataset import LabDataset
from stonks.lab.objectives import FinalReturnObjective
from stonks.lab.survival.oos import OutOfSampleTest
from stonks.lab.survival.period_stability import PeriodStabilityTest
from stonks.lab.survival.perturbation import PerturbationTest
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.buy_and_hold import BuyAndHold

TICKER = "AAPL.US"


@pytest.fixture
def lake_hourly(tmp_path):
    """Six calendar days of hourly bars (8 per day), price rising 100 → ~124."""
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    rows = []
    i = 0
    for day in range(6):
        session = datetime(2026, 4, 1, 14, 0, tzinfo=UTC) + timedelta(days=day)
        for hour in range(8):
            close = 100.0 + 0.5 * i
            rows.append(
                {
                    "ticker": TICKER,
                    "timestamp": session + timedelta(hours=hour),
                    "open": close - 0.1,
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "adj_close": close,
                    "volume": 1_000,
                }
            )
            i += 1
    lake.upsert_bars(pd.DataFrame(rows), interval=Interval.HOUR_1)
    yield lake
    lake.close()


def _dataset(lake) -> LabDataset:
    return LabDataset(
        lake=lake,
        universe=[TICKER],
        start=date(2026, 4, 1),
        end=date(2026, 4, 6),
        train_ratio=0.5,
        interval=Interval.HOUR_1,
    )


def _strategy() -> BuyAndHold:
    return BuyAndHold({"ticker": TICKER, "allocation": 1.0})


def test_objective_backtests_at_dataset_interval(lake_hourly):
    score = FinalReturnObjective().score(_strategy(), _dataset(lake_hourly))
    assert score > 0.0


def test_oos_backtests_at_dataset_interval(lake_hourly):
    report = OutOfSampleTest(min_sharpe=-1e9, max_drawdown_limit=-1.0).run(
        _strategy(), _dataset(lake_hourly)
    )
    assert report.metrics["final_return_oos"] > 0.0


def test_period_stability_backtests_at_dataset_interval(lake_hourly):
    report = PeriodStabilityTest(n_windows=2, max_sharpe_std=1e9).run(
        _strategy(), _dataset(lake_hourly)
    )
    assert report.metrics["sharpe_min"] > 0.0


def test_perturbation_backtests_at_dataset_interval(lake_hourly):
    report = PerturbationTest(noise_sigmas=[0.001], min_correlation=-1.0).run(
        _strategy(), _dataset(lake_hourly)
    )
    # an empty equity curve would yield a correlation of 0.0 (the level
    # correlation: a linear price path has near-constant returns, so the
    # return correlation under noise says nothing here)
    assert report.metrics["level_correlation_min"] > 0.9
