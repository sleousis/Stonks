"""Backtester with execution realism (BL-30, BL-31): participation cap and
carried remainders, zero-volume bars, stop orders, the gap guard, lagged
market statistics with a warm-up, partial fills in the trade ledger, T+1
settlement, and golden runs pinning both the default and the new models."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.costs import AssetClassCosts, CostModelSettings, Trade, TradeCost
from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.fills import (
    ExecutionSettings,
    FillModelSettings,
    MarketStatsSpec,
)
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.backtest.trades import with_trades
from stonks.core.interval import Interval
from stonks.core.types import Order, Portfolio
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.parallel_lab import random_walk_lake

_D0 = date(2026, 1, 5)  # a Monday


def _day(i: int) -> datetime:
    return datetime.combine(_D0 + timedelta(days=i), datetime.min.time(), tzinfo=UTC)


def _row(ticker: str, i: int, open_: float, *, close=None, high=None, low=None, volume=1_000.0):
    close = open_ if close is None else close
    return {
        "ticker": ticker,
        "timestamp": _day(i),
        "open": open_,
        "high": max(open_, close) + 1.0 if high is None else high,
        "low": min(open_, close) - 1.0 if low is None else low,
        "close": close,
        "adj_close": close,
        "volume": volume,
    }


def _lake(tmp_path, rows: list[dict]) -> DuckDBLake:
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    lake.upsert_bars(pd.DataFrame(rows), interval=Interval.DAY_1)
    return lake


def _config(start: int, end: int, universe=("X.US",), **kw: Any) -> BacktestConfig:
    return BacktestConfig(
        start=_day(start), end=_day(end), universe=list(universe), rebalance_every_bars=1_000, **kw
    )


class _Once:
    """Emits ``orders`` on its first decision only."""

    id = "once"
    applicable_asset_classes = ("equity",)

    def __init__(self, orders: Sequence[Order], later: Mapping[int, Sequence[Order]] = {}) -> None:
        self._orders = list(orders)
        self._later = dict(later)
        self._calls = 0

    def estimate_return(self, ticker, as_of, lake):
        return None

    def decide(self, my_picks, portfolio, prices, as_of) -> list[Order]:
        self._calls += 1
        if self._calls == 1:
            return list(self._orders)
        return list(self._later.get(self._calls, []))


def _buy(qty: float, cid="b", kind="market", limit=None, side="buy") -> Order:
    return Order(
        client_id=cid, ticker="X.US", side=side, quantity=qty, order_type=kind, limit_price=limit
    )


def _bar_broker(cash=1e6, positions=None, fill=None, **kw) -> SimulatedBroker:
    return SimulatedBroker.from_execution(
        Portfolio(cash=cash, positions=dict(positions or {})),
        ExecutionSettings(fill=fill or FillModelSettings(), **kw.pop("execution", {})),
        **kw,
    )


# ---- participation cap ----------------------------------------------------------------


def test_ten_percent_cap_splits_an_order_across_bars(tmp_path):
    lake = _lake(tmp_path, [_row("X.US", i, 10.0 + i) for i in range(6)])
    broker = _bar_broker()
    Backtester([_Once([_buy(250.0)])], broker, lake, _config(0, 5)).run()
    fills = broker.fills
    assert [f.quantity for f in fills] == pytest.approx([100.0, 100.0, 50.0])
    assert [f.price for f in fills] == pytest.approx([11.0, 12.0, 13.0])  # each bar's open
    assert [f.order_client_id for f in fills] == ["0:b", "0:b~2", "0:b~3"]
    assert broker.fetch_portfolio().positions["X.US"] == pytest.approx(250.0)
    lake.close()


def test_a_new_rebalance_replaces_the_carried_remainder(tmp_path):
    lake = _lake(tmp_path, [_row("X.US", i, 10.0) for i in range(6)])
    broker = _bar_broker()
    config = BacktestConfig(start=_day(0), end=_day(5), universe=["X.US"], rebalance_every_bars=2)
    strategy = _Once([_buy(1_000.0)])  # later decisions are empty
    Backtester([strategy], broker, lake, config).run()
    # bars 1 and 2 fill 100 each; bar 2 then re-decides (nothing) and drops the rest
    assert [f.quantity for f in broker.fills] == pytest.approx([100.0, 100.0])
    lake.close()


def test_zero_volume_bar_never_fills_and_the_order_waits(tmp_path):
    rows = [_row("X.US", 0, 10.0), _row("X.US", 1, 11.0, volume=0.0), _row("X.US", 2, 12.0)]
    lake = _lake(tmp_path, rows)
    broker = _bar_broker()
    Backtester([_Once([_buy(50.0)])], broker, lake, _config(0, 2)).run()
    (fill,) = broker.fills
    assert fill.price == pytest.approx(12.0)
    assert fill.quantity == pytest.approx(50.0)
    lake.close()


# ---- order types and gaps -------------------------------------------------------------


def test_untouched_limit_does_not_fill_and_expires(tmp_path):
    rows = [_row("X.US", i, 10.0, high=10.5, low=9.5) for i in range(4)]
    lake = _lake(tmp_path, rows)
    broker = _bar_broker()
    Backtester([_Once([_buy(10.0, kind="limit", limit=9.0)])], broker, lake, _config(0, 3)).run()
    assert broker.fills == ()
    lake.close()


def test_gapped_stop_fills_at_the_open(tmp_path):
    rows = [
        _row("X.US", 0, 100.0),
        _row("X.US", 1, 80.0, high=82.0, low=78.0),  # gaps through the 90 stop
        _row("X.US", 2, 80.0),
    ]
    lake = _lake(tmp_path, rows)
    broker = _bar_broker(positions={"X.US": 10.0})
    stop = _buy(10.0, kind="stop", limit=90.0, side="sell")
    Backtester([_Once([stop])], broker, lake, _config(0, 2)).run()
    (fill,) = broker.fills
    assert fill.side == "sell" and fill.price == pytest.approx(80.0)
    lake.close()


def test_no_fill_after_a_gap_longer_than_max_gap_days(tmp_path):
    rows = [_row("X.US", 0, 10.0), _row("X.US", 30, 10.0), _row("Y.US", 1, 5.0)]
    lake = _lake(tmp_path, rows)
    broker = _bar_broker()
    Backtester([_Once([_buy(1.0)])], broker, lake, _config(0, 30, universe=("X.US", "Y.US"))).run()
    assert broker.fills == ()
    lake.close()


def test_the_legacy_broker_fills_after_the_same_gap(tmp_path):
    rows = [_row("X.US", 0, 10.0), _row("X.US", 30, 10.0)]
    lake = _lake(tmp_path, rows)
    broker = SimulatedBroker(Portfolio(cash=1e6))
    Backtester([_Once([_buy(1.0)])], broker, lake, _config(0, 30)).run()
    assert len(broker.fills) == 1
    lake.close()


# ---- lagged market statistics ---------------------------------------------------------


class _SpyCosts:
    market_stats_spec = MarketStatsSpec(adv_window=3, vol_window=3)

    def __init__(self) -> None:
        self.trades: list[Trade] = []

    def cost(self, trade: Trade) -> TradeCost:
        self.trades.append(trade)
        return TradeCost(fill_price=trade.price, fee=0.0)


def _volume_rows(fill_bar_volume: float) -> list[dict]:
    volumes = [100.0, 200.0, 300.0, 400.0, 500.0, fill_bar_volume, 700.0]
    return [_row("X.US", i, 10.0 + i, volume=v) for i, v in enumerate(volumes)]


def test_stats_are_lagged_and_warm_up_before_the_window(tmp_path):
    lake = _lake(tmp_path, _volume_rows(600.0))
    spy = _SpyCosts()
    broker = SimulatedBroker(Portfolio(cash=1e6), cost_model=spy)
    # window starts at bar 4: bars 1-3 are warm-up only
    report = Backtester([_Once([_buy(1.0)])], broker, lake, _config(4, 6)).run()
    assert len(report.equity_curve) == 3  # warm-up bars are not on the timeline
    (trade,) = spy.trades  # decided on bar 4, fills at bar 5's open
    assert trade.price == pytest.approx(15.0)
    assert trade.adv == pytest.approx(400.0)  # median of bars 2-4, not bar 5
    closes = [11.0, 12.0, 13.0, 14.0]  # bars 1-4: three returns, none from bar 5
    assert trade.sigma_daily == pytest.approx(np.std(np.diff(np.log(closes)), ddof=1))
    lake.close()


def test_changing_the_fill_bar_does_not_change_its_adv(tmp_path):
    advs = []
    for i, volume in enumerate((600.0, 6e9)):
        lake = _lake(tmp_path / str(i), _volume_rows(volume))
        spy = _SpyCosts()
        broker = SimulatedBroker(Portfolio(cash=1e6), cost_model=spy)
        Backtester([_Once([_buy(1.0)])], broker, lake, _config(4, 6)).run()
        advs.append((spy.trades[0].adv, spy.trades[0].sigma_daily))
        lake.close()
    assert advs[0] == advs[1]


def test_default_engine_still_loads_prices_with_a_single_query(tmp_path, monkeypatch):
    lake = _lake(tmp_path, _volume_rows(600.0))
    calls: list[str] = []
    real_sql = lake.sql
    monkeypatch.setattr(lake, "sql", lambda q, p=None: calls.append(q) or real_sql(q, p))
    broker = _bar_broker(cost_model=CostModelSettings(impact_model="istar").build())
    Backtester([_Once([_buy(1.0)])], broker, lake, _config(4, 6)).run()
    assert len(calls) == 1
    lake.close()


# ---- the trade ledger over partial fills ----------------------------------------------


def test_partial_fills_pair_into_round_trips_with_their_own_costs(tmp_path):
    rows = [_row("X.US", i, 10.0 + i) for i in range(8)]
    lake = _lake(tmp_path, rows)
    broker = _bar_broker(
        cost_model=CostModelSettings(
            default=AssetClassCosts(half_spread_bps=10.0, fee_flat=1.0)
        ).build()
    )
    sell = Order("s", "X.US", "sell", 250.0)
    strategy = _Once([_buy(250.0)], later={2: [sell]})
    config = BacktestConfig(start=_day(0), end=_day(7), universe=["X.US"], rebalance_every_bars=4)
    report = Backtester([strategy], broker, lake, config).run()
    report = with_trades(report, broker.fills, reference_price=broker.reference_price)
    buys = [f for f in broker.fills if f.side == "buy"]
    sells = [f for f in broker.fills if f.side == "sell"]
    assert [f.quantity for f in buys] == pytest.approx([100.0, 100.0, 50.0])
    assert [f.quantity for f in sells] == pytest.approx([100.0, 100.0, 50.0])
    closed = [t for t in report.trades if not t.is_open]
    assert sum(t.qty for t in closed) == pytest.approx(250.0)
    assert not [t for t in report.trades if t.is_open]
    assert sum(t.fees for t in closed) == pytest.approx(6.0)  # one flat fee per fill
    expected_slip = sum(
        abs(f.price - broker.reference_price(f.order_client_id)) * f.quantity for f in broker.fills
    )
    assert sum(t.slippage_cost for t in closed) == pytest.approx(expected_slip)
    assert report.trade_stats.n_trades == len(closed)
    lake.close()


# ---- settlement -----------------------------------------------------------------------


def test_t_plus_one_delays_reinvesting_sale_proceeds(tmp_path):
    rows = [_row(t, i, 10.0) for t in ("X.US", "Y.US") for i in range(5)]
    lake = _lake(tmp_path, rows)
    orders = [Order("s", "X.US", "sell", 100.0), Order("b", "Y.US", "buy", 100.0)]
    results = {}
    for days in (0, 1):
        broker = SimulatedBroker(
            Portfolio(cash=0.0, positions={"X.US": 100.0}), settlement_days=days
        )
        Backtester([_Once(orders)], broker, lake, _config(0, 4, universe=("X.US", "Y.US"))).run()
        results[days] = [f.order_client_id for f in broker.fills]
    assert results[0] == ["0:s", "0:b"]
    assert results[1] == ["0:s"]  # the buy is rejected on unsettled cash
    lake.close()


# ---- golden runs ----------------------------------------------------------------------


def _golden_rows() -> list[dict]:
    rng = np.random.default_rng(42)
    rows = []
    for ticker in ("A.US", "B.US"):
        price = 50.0
        for i in range(60):
            open_ = price * float(np.exp(rng.normal(0, 0.01)))
            close = open_ * float(np.exp(rng.normal(0, 0.02)))
            rows.append(
                _row(
                    ticker,
                    i,
                    round(open_, 4),
                    close=round(close, 4),
                    high=round(max(open_, close) * 1.01, 4),
                    low=round(min(open_, close) * 0.99, 4),
                    volume=float(rng.integers(0, 3)) * 500.0,
                )
            )
            price = close
    return rows


def _golden_run(lake, broker) -> tuple[list[float], list[tuple[str, float, float]]]:
    strategy = BuyAndHold({"ticker": "A.US", "allocation": 1.0})
    config = BacktestConfig(
        start=_day(30), end=_day(59), universe=["A.US", "B.US"], rebalance_every_bars=10
    )
    report = Backtester([strategy], broker, lake, config).run()
    return report.equity_curve, [(f.order_client_id, f.quantity, f.price) for f in broker.fills]


def test_explicit_default_execution_matches_the_legacy_broker(tmp_path):
    lake = _lake(tmp_path, _golden_rows())
    costs = CostModelSettings.realistic()
    legacy = _golden_run(lake, SimulatedBroker(Portfolio(cash=10_000.0), cost_model=costs.build()))
    explicit = _golden_run(
        lake,
        SimulatedBroker.from_execution(
            Portfolio(cash=10_000.0), ExecutionSettings(), cost_model=costs.build()
        ),
    )
    assert explicit == legacy
    assert len(legacy[1]) == 1  # the default fills the whole order at once
    lake.close()


def test_golden_bar_fills_with_istar_and_corwin_schultz(tmp_path):
    lake = _lake(tmp_path, _golden_rows())
    costs = CostModelSettings.realistic().model_copy(
        update={"impact_model": "istar", "half_spread_model": "corwin_schultz"}
    )
    broker = _bar_broker(cash=10_000.0, cost_model=costs.build())
    equity, fills = _golden_run(lake, broker)
    again = _golden_run(lake, _bar_broker(cash=10_000.0, cost_model=costs.build()))
    assert (equity, fills) == again  # deterministic
    # pinned: the buy is capped at 10% of a 1,000 / 500 share bar, waits
    # out a zero-volume bar (part ~3 never fills), and its last part is
    # scaled to the remaining cash
    assert [cid.partition("~")[2] or "1" for cid, _, _ in fills] == _GOLDEN_PARTS
    assert [q for _, q, _ in fills] == pytest.approx(_GOLDEN_QTYS, rel=1e-9)
    assert [p for _, _, p in fills] == pytest.approx(_GOLDEN_PRICES, rel=1e-9)
    assert equity[-1] == pytest.approx(8732.100029864589, rel=1e-9)
    lake.close()


_GOLDEN_PARTS = ["1", "2", "4"]
_GOLDEN_QTYS = [100.0, 50.0, 43.61555003890425]
_GOLDEN_PRICES = [52.11387451287523, 52.00487954449391, 50.162581797890766]


@pytest.mark.parametrize("backend", ["duckdb", "parquet"])
def test_warm_up_query_works_on_both_bar_backends(tmp_path, backend):
    lake = random_walk_lake(tmp_path / "lake.duckdb", ["A.US"], periods=80)
    if backend == "parquet":
        lake.migrate_bars_to_parquet()
    spy = _SpyCosts()
    broker = SimulatedBroker(Portfolio(cash=1e6), cost_model=spy)
    config = BacktestConfig(start=date(2024, 3, 1), end=date(2024, 4, 19), universe=["A.US"])
    strategy = BuyAndHold({"ticker": "A.US", "allocation": 0.5})
    report = Backtester([strategy], broker, lake, config).run()
    assert report.equity_dates[0].date() >= date(2024, 3, 1)
    (trade,) = spy.trades
    assert trade.adv is not None and trade.sigma_daily is not None  # warmed up
    lake.close()


# ---- RS-14: the gap guard scales with the bar length -------------------------------


@pytest.mark.parametrize(
    ("interval", "step_days"), [(Interval.WEEK_1, 7), (Interval.MONTH_1, 31)], ids=["1w", "1mo"]
)
def test_weekly_and_monthly_backtests_still_fill(tmp_path, interval, step_days):
    rows = [_row("X.US", i * step_days, 10.0 + i) for i in range(4)]
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    lake.upsert_bars(pd.DataFrame(rows), interval=interval)
    broker = _bar_broker()
    config = BacktestConfig(
        start=_day(0),
        end=_day(3 * step_days),
        universe=["X.US"],
        interval=interval,
        rebalance_every_bars=1_000,
    )
    Backtester([_Once([_buy(1.0)])], broker, lake, config).run()
    assert len(broker.fills) == 1
    lake.close()


def test_a_missing_monthly_bar_beyond_two_bars_still_expires(tmp_path):
    rows = [_row("X.US", 0, 10.0), _row("X.US", 95, 11.0)]
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    lake.upsert_bars(pd.DataFrame(rows), interval=Interval.MONTH_1)
    broker = _bar_broker()
    config = BacktestConfig(
        start=_day(0),
        end=_day(95),
        universe=["X.US"],
        interval=Interval.MONTH_1,
        rebalance_every_bars=1_000,
    )
    Backtester([_Once([_buy(1.0)])], broker, lake, config).run()
    assert broker.fills == ()
    lake.close()


def test_a_limit_order_carried_by_the_participation_cap_keeps_its_limit(tmp_path):
    """Edge case: the carried child of a capped limit order is still a limit
    order. It fills while the bar reaches the limit, then expires (a DAY
    order) on the first bar whose range stays above it."""
    lake = _lake(tmp_path, [_row("X.US", i, 10.0 + i) for i in range(5)])
    broker = _bar_broker()
    order = _buy(250.0, kind="limit", limit=11.5)
    Backtester([_Once([order])], broker, lake, _config(0, 4)).run()
    fills = broker.fills
    assert [f.quantity for f in fills] == pytest.approx([100.0, 100.0])
    # bar 1 opens below the limit; bar 2's range touches it; bar 3's low is above
    assert [f.price for f in fills] == pytest.approx([11.0, 11.5])
    lake.close()
