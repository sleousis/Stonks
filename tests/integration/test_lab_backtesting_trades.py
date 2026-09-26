"""The lab's backtests carry the round-trip trade ledger (BL-02).

The strongest check is the accounting identity: with every round trip
(open lots marked at the last close), the sum of trade P&L equals the
change in equity, whatever the costs, splits and dividends."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.costs import AssetClassCosts, CostModelSettings
from stonks.core.interval import Interval
from stonks.core.types import Order, Portfolio
from stonks.lab.backtesting import LAB_INITIAL_CASH, run_backtest, run_backtest_with_fills
from stonks.lab.dataset import LabDataset
from stonks.store.lake import DuckDBLake
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples.momentum import Momentum

DAYS = [d.date() for d in pd.bdate_range("2024-06-03", periods=12)]


def _lake(tmp_path, closes, *, days=DAYS, ticker="X.US") -> DuckDBLake:
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    closes = np.asarray(closes, dtype=float)
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": ticker,
                "date": days[: len(closes)],
                "open": closes,
                "high": closes * 1.02,
                "low": closes * 0.97,
                "close": closes,
                "adj_close": closes,
                "volume": 1_000_000.0,
            }
        )
    )
    return lake


def _dataset(lake, days=DAYS, costs=None) -> LabDataset:
    return LabDataset(
        lake=lake,
        universe=["X.US"],
        start=days[0],
        end=days[-1],
        interval=Interval.DAY_1,
        costs=costs,
    )


class _Script(BaseStrategy):
    """Buys ``buys[day]`` shares and sells ``sells[day]`` shares (decided on
    ``day``, filled at the next open)."""

    id = "script"

    @classmethod
    def parameter_spec(cls):
        return []

    def __init__(self, buys: Mapping[date, float], sells: Mapping[date, float]) -> None:
        super().__init__({})
        self._buys, self._sells = dict(buys), dict(sells)

    def estimate_return(self, ticker, as_of, lake):
        return 1.0

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of,
    ) -> list[Order]:
        day = as_of.date() if isinstance(as_of, datetime) else as_of
        orders = []
        if day in self._buys:
            orders.append(Order(f"buy:{day}", "X.US", "buy", self._buys[day]))
        if day in self._sells:
            orders.append(Order(f"sell:{day}", "X.US", "sell", self._sells[day]))
        return orders


def _assert_pnl_identity(report):
    total = sum(t.pnl for t in report.trades)
    assert total == pytest.approx(report.equity_curve[-1] - LAB_INITIAL_CASH, abs=1e-6)


def test_run_backtest_attaches_the_ledger_with_excursions(tmp_path):
    closes = [100.0, 100.0, 105.0, 110.0, 108.0, 112.0, 115.0, 111.0, 109.0, 113.0, 114.0, 116.0]
    lake = _lake(tmp_path, closes)
    strat = _Script(buys={DAYS[0]: 10.0, DAYS[1]: 10.0}, sells={DAYS[4]: 15.0})
    report = run_backtest(strat, _dataset(lake), (DAYS[0], DAYS[-1]))

    assert [(t.qty, t.is_open) for t in report.trades] == [(10.0, False), (5.0, False), (5.0, True)]
    first = report.trades[0]
    assert (first.entry_px, first.exit_px) == (100.0, 112.0)  # next-bar opens
    assert first.mae_pct == pytest.approx(0.97 - 1.0)
    assert first.mfe_pct == pytest.approx(112.0 * 1.02 / 100.0 - 1.0)
    assert report.trades[-1].exit_px == 116.0  # marked at the last close
    assert report.trade_stats.n_trades == 2 and report.trade_stats.n_open == 1
    assert report.fitness is not None
    _assert_pnl_identity(report)
    lake.close()


def test_pnl_identity_holds_with_costs(tmp_path):
    costs = CostModelSettings(default=AssetClassCosts(fee_flat=1.5, half_spread_bps=20.0))
    rng = np.random.default_rng(5)
    days = [d.date() for d in pd.bdate_range("2024-01-02", periods=120)]
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.02, len(days))))
    lake = _lake(tmp_path, closes, days=days)
    strat = Momentum({"lookback_days": 5, "threshold": 0.0, "allocation": 1.0})
    report, fills = run_backtest_with_fills(strat, _dataset(lake, days, costs), (days[0], days[-1]))

    assert report.trade_stats.n_trades > 3
    assert report.trade_stats.costs_paid > 1.5 * len(fills)  # fees plus slippage
    assert sum(t.slippage_cost for t in report.trades) > 0
    _assert_pnl_identity(report)
    lake.close()


def test_pnl_identity_holds_through_a_split_and_a_dividend(tmp_path):
    closes = [400.0] * 5 + [100.0] * 7
    lake = _lake(tmp_path, closes)
    lake.upsert_stock_splits(pd.DataFrame([{"ticker": "X.US", "date": DAYS[5], "ratio": 4.0}]))
    lake.upsert_dividends(
        pd.DataFrame(
            [
                {
                    "ticker": "X.US",
                    "ex_date": DAYS[8],
                    "amount": 1.0,
                    "currency": None,
                    "pay_date": None,
                    "record_date": None,
                    "declaration_date": None,
                }
            ]
        )
    )
    # 10 shares before the split = 40 after; sell 30 after the dividend
    strat = _Script(buys={DAYS[0]: 10.0}, sells={DAYS[9]: 30.0})
    report = run_backtest(strat, _dataset(lake), (DAYS[0], DAYS[-1]))

    closed, still_open = report.trades
    assert closed.qty == pytest.approx(30.0) and closed.entry_px == pytest.approx(100.0)
    assert closed.dividends == pytest.approx(30.0)
    assert closed.mae_pct == pytest.approx(0.97 - 1.0)  # not the raw 75% "crash"
    assert still_open.is_open and still_open.qty == pytest.approx(10.0)
    _assert_pnl_identity(report)
    lake.close()


def test_run_backtest_without_fills_has_an_empty_ledger(tmp_path):
    lake = _lake(tmp_path, [100.0] * 12)
    report = run_backtest(_Script({}, {}), _dataset(lake), (DAYS[0], DAYS[-1]))
    assert report.trades == ()
    assert report.trade_stats.n_trades == 0
    assert report.fitness == 0.0
    lake.close()


def test_two_engine_instances_keep_separate_ledgers(tmp_path):
    from stonks.backtest.engine import BacktestConfig, Backtester
    from stonks.backtest.simulated_broker import SimulatedBroker
    from stonks.backtest.trades import with_trades

    closes = [100.0 + i for i in range(12)]
    lake = _lake(tmp_path, closes)
    a = _Script(buys={DAYS[0]: 10.0}, sells={DAYS[3]: 10.0})
    b = _Script(buys={DAYS[2]: 5.0}, sells={})
    broker = SimulatedBroker(Portfolio(cash=10_000.0))
    config = BacktestConfig(start=DAYS[0], end=DAYS[-1], universe=["X.US"])
    report = Backtester([a, b], broker, lake, config).run()
    report = with_trades(report, broker.fills, reference_price=broker.reference_price)

    by_key = {t.strategy_key: t for t in report.trades}
    assert set(by_key) == {"0", "1"}
    assert not by_key["0"].is_open and by_key["0"].qty == 10.0
    assert by_key["1"].is_open and by_key["1"].qty == 5.0
    lake.close()
