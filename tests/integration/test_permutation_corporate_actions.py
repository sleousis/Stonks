"""Permuted lakes must stay consistent with corporate actions.

Raw bars carry a split as a price drop on its ex-date; the backtest engine
multiplies the held quantity on that date. Shuffling raw bars moves the
drop to a random bar while the split row stays put, so the engine would
multiply the position on a date where the price no longer falls (equity
jumps ~10x). Permutations therefore shuffle *adjusted* bars and the
modified lakes carry no corporate-action rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.lab.backtesting import run_backtest
from stonks.lab.dataset import LabDataset
from stonks.lab.survival.permutation import PermutationScorer, permutation_seeds
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.buy_and_hold import BuyAndHold

SPLIT_RATIO = 10.0


@pytest.fixture
def split_lake(tmp_path):
    """GBM prices with a 10:1 split halfway through: raw closes on and
    after the ex-date are a tenth of the pre-split level."""
    rng = np.random.default_rng(11)
    n = 200
    dates = pd.bdate_range(start="2025-01-02", periods=n)
    close = np.exp(np.log(100.0) + np.cumsum(rng.normal(0.0, 0.01, n)))
    ex_idx = 150
    raw = close.copy()
    raw[ex_idx:] /= SPLIT_RATIO
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": "SPL.US",
                "date": [d.date() for d in dates],
                "open": raw,
                "high": raw,
                "low": raw,
                "close": raw,
                "adj_close": raw,
                "volume": 1_000_000,
            }
        )
    )
    lake.upsert_stock_splits(
        pd.DataFrame([{"ticker": "SPL.US", "date": dates[ex_idx].date(), "ratio": SPLIT_RATIO}])
    )
    lake.upsert_dividends(
        pd.DataFrame(
            [
                {
                    "ticker": "SPL.US",
                    "ex_date": dates[160].date(),
                    "amount": 0.05,
                    "currency": "USD",
                    "pay_date": None,
                    "record_date": None,
                    "declaration_date": None,
                }
            ]
        )
    )
    yield lake, dates
    lake.close()


@dataclass(frozen=True)
class _MaxEquityJump:
    """Evaluator: the largest absolute one-bar log change of the equity
    curve of a buy-and-hold run, plus a check the lake has no events."""

    window: tuple[date, date]

    def __call__(self, strategy, dataset) -> float:
        events = dataset.lake.get_corporate_actions(["SPL.US"])
        assert events.empty, "modified lakes must not carry corporate actions"
        equity = np.asarray(run_backtest(strategy, dataset, self.window).equity_curve)
        return float(np.max(np.abs(np.diff(np.log(equity)))))


def test_permuted_equity_has_no_split_jump(split_lake):
    lake, dates = split_lake
    ds = LabDataset(
        lake=lake,
        universe=["SPL.US"],
        start=dates[0].date(),
        end=dates[-1].date(),
        train_ratio=0.6,
        interval=Interval.DAY_1,
    )
    window = (dates[100].date(), dates[-1].date())  # contains the split
    strategy = BuyAndHold({"ticker": "SPL.US", "allocation": 1.0})
    scorer = PermutationScorer.build(strategy, ds, window, _MaxEquityJump(window))
    assert scorer is not None

    # per-bar moves are ~1% (sigma 0.01); a misapplied split is log(10) ~ 2.3
    assert scorer.score_real() < 0.1
    for seed in permutation_seeds(8, 3):
        assert scorer.score_permutation(seed) < 0.1


def test_real_backtest_on_the_source_lake_is_smooth_too(split_lake):
    """Sanity: the engine itself handles the split on the unmodified lake."""
    lake, dates = split_lake
    ds = LabDataset(
        lake=lake,
        universe=["SPL.US"],
        start=dates[0].date(),
        end=dates[-1].date(),
        interval=Interval.DAY_1,
    )
    report = run_backtest(
        BuyAndHold({"ticker": "SPL.US", "allocation": 1.0}),
        ds,
        (dates[100].date(), dates[-1].date()),
    )
    equity = np.asarray(report.equity_curve)
    assert np.max(np.abs(np.diff(np.log(equity)))) < 0.1
