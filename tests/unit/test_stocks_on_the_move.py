"""Stocks on the Move (Clenow): regression momentum x R², trend and gap
filters, index filter, ATR risk parity, weekly trading."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.interval import Interval
from stonks.core.types import Portfolio
from stonks.features.indicators import atr
from stonks.lab.catalog import strategy_catalog
from stonks.store.lake import DuckDBLake
from stonks.strategies.base import strategy_metadata
from stonks.strategies.examples.stocks_on_the_move import StocksOnTheMove

DATES = pd.bdate_range("2023-01-02", "2024-07-31")
WED = date(2024, 6, 12)
THU = WED + timedelta(days=1)
N = len(DATES)
T_WED = int(np.searchsorted(DATES, pd.Timestamp(WED)))


def _smooth(daily: float) -> np.ndarray:
    return 50.0 * np.exp(daily * np.arange(N))


def _gap() -> np.ndarray:
    closes = _smooth(0.003)
    closes[T_WED - 30 :] *= 1.20  # a 20% jump 30 sessions before WED
    return closes


def _below_ma() -> np.ndarray:
    closes = _smooth(0.004)
    # a slide over the last 12 sessions that leaves it under its 100-day mean
    closes[T_WED - 11 : T_WED + 1] *= np.linspace(0.97, 0.80, 12)
    closes[T_WED + 1 :] *= 0.80
    return closes


def _series() -> dict[str, np.ndarray]:
    out = {f"S{i}.US": _smooth(0.0005 * (i + 1)) for i in range(6)}
    out["DOWN.US"] = _smooth(-0.001)
    out["GAP.US"] = _gap()
    out["BELOW.US"] = _below_ma()
    out["FLAT.US"] = np.full(N, 20.0)
    return out


def _frame(ticker: str, closes: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": ticker,
            "date": [d.date() for d in DATES],
            "open": closes,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "adj_close": closes,
            "volume": 1_000_000,
        }
    )


def _lake(path, series: dict[str, np.ndarray]) -> DuckDBLake:
    lake = DuckDBLake(path)
    lake.migrate()
    lake.upsert_prices(pd.concat([_frame(t, c) for t, c in series.items()], ignore_index=True))
    return lake


def _index(bull: bool) -> np.ndarray:
    return _smooth(0.0005) if bull else _smooth(-0.0005)


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    lake = _lake(
        tmp_path_factory.mktemp("sotm") / "lake.duckdb", {**_series(), "SPY.US": _index(True)}
    )
    yield lake
    lake.close()


@pytest.fixture(scope="module")
def bear_lake(tmp_path_factory):
    lake = _lake(
        tmp_path_factory.mktemp("sotm_bear") / "lake.duckdb", {**_series(), "SPY.US": _index(False)}
    )
    yield lake
    lake.close()


STOCKS = list(_series())


def _evaluate(s, lake, as_of=WED):
    return {t: s.estimate_return(t, as_of, lake) for t in STOCKS}


# --- catalog and metadata ---------------------------------------------------


def test_catalogued_with_metadata():
    assert strategy_catalog()["stocks_on_the_move"] is StocksOnTheMove
    meta = strategy_metadata(StocksOnTheMove)
    assert meta.alpha_family == "trend"
    assert meta.premise == "trend"
    assert meta.required_history_bars == 200
    assert meta.applicable_asset_classes == ("equity",)
    assert meta.hypothesis


# --- ranking and filters --------------------------------------------------------


def test_scores_rank_steady_trends_by_slope(lake):
    s = StocksOnTheMove({"top_fraction": 1.0})
    scores = s.score_universe(STOCKS, WED, lake)
    ranked = sorted((t for t in scores if t.startswith("S")), key=lambda t: -scores[t])
    assert ranked == [f"S{i}.US" for i in reversed(range(6))]
    expected = np.exp(0.003 * 250) - 1  # S5: clean exponential, R² = 1
    assert scores["S5.US"] == pytest.approx(expected, rel=1e-6)
    assert "DOWN.US" not in scores and "FLAT.US" not in scores


def test_gap_filter(lake):
    assert "GAP.US" not in StocksOnTheMove({"top_fraction": 1.0}).score_universe(STOCKS, WED, lake)
    loose = StocksOnTheMove({"top_fraction": 1.0, "gap_limit": 0.25})
    assert "GAP.US" in loose.score_universe(STOCKS, WED, lake)


def test_moving_average_filter(lake):
    s = StocksOnTheMove({"top_fraction": 1.0})
    stats = s._stats("BELOW.US", WED, lake)
    assert stats.score > 0 and not stats.above_ma
    assert "BELOW.US" not in s.score_universe(STOCKS, WED, lake)


def test_top_fraction_ranks_the_whole_universe_before_filtering(lake):
    # 10 names with history -> the top 2 by score are GAP and S5. GAP fails
    # the gap filter but keeps its slot, so S4 does not move up.
    assert set(StocksOnTheMove({}).score_universe(STOCKS, WED, lake)) == {"S5.US"}
    loose = StocksOnTheMove({"gap_limit": 0.5})
    assert set(loose.score_universe(STOCKS, WED, lake)) == {"GAP.US", "S5.US"}


def test_estimate_return_matches_score_universe(lake):
    s = StocksOnTheMove({"top_fraction": 1.0, "universe": ",".join(STOCKS)})
    got = {t: v for t, v in _evaluate(s, lake).items() if v is not None}
    assert got == s.score_universe(STOCKS, WED, lake)


def test_the_index_is_not_part_of_the_default_universe(lake):
    s = StocksOnTheMove({"top_fraction": 1.0, "index_ticker": "SPY.US"})
    assert s.estimate_return("SPY.US", WED, lake) is None
    assert s.estimate_return("S5.US", WED, lake) is not None


def test_future_bars_never_change_a_score(tmp_path, lake):
    series = {t: c.copy() for t, c in _series().items()}
    for c in series.values():
        c[T_WED + 1 :] *= 0.5
    other = _lake(tmp_path / "future.duckdb", {**series, "SPY.US": _index(True)})
    try:
        s = StocksOnTheMove({"top_fraction": 1.0})
        assert s.score_universe(STOCKS, WED, other) == s.score_universe(STOCKS, WED, lake)
    finally:
        other.close()


# --- decide: timing, index filter, sizing ------------------------------------------


def _bars_atr(lake, ticker, as_of=WED, period=20):
    bars = lake.get_bars(ticker, Interval.DAY_1, start=date(2024, 1, 1), end=as_of)
    return float(atr(bars["high"], bars["low"], bars["close"], period, method="sma").iloc[-1])


def _prices(lake, as_of=WED):
    return {
        t: float(lake.get_bars(t, Interval.DAY_1, start=as_of, end=as_of)["close"].iloc[-1])
        for t in STOCKS
    }


def test_decide_trades_only_on_the_weekly_session(lake):
    s = StocksOnTheMove({})
    picks = [(v, t) for t, v in _evaluate(s, lake).items() if v is not None]
    assert picks
    book = Portfolio(cash=100_000.0, positions={})
    assert s.decide(picks, book, _prices(lake), THU) == []
    assert s.decide(picks, book, _prices(lake), WED)


def test_a_holiday_wednesday_moves_the_trade_to_thursday():
    s = StocksOnTheMove({})
    book = Portfolio(cash=0.0, positions={"A": 1.0})
    # 2024-06-19 (Wednesday) is Juneteenth; nothing is picked, so A is sold
    assert s.decide([], book, {"A": 10.0}, date(2024, 6, 20))
    assert s.decide([], book, {"A": 10.0}, date(2024, 6, 21)) == []


def test_atr_parity_sizing(lake):
    s = StocksOnTheMove({})
    picks = [(v, t) for t, v in _evaluate(s, lake).items() if v is not None]
    prices = _prices(lake)
    orders = s.decide(picks, Portfolio(cash=100_000.0, positions={}), prices, WED)
    assert orders and all(o.side == "buy" for o in orders)
    for o in orders:
        assert o.quantity == pytest.approx(100_000.0 * 0.001 / _bars_atr(lake, o.ticker), rel=1e-6)


def test_buys_never_exceed_cash(lake):
    s = StocksOnTheMove({"risk_factor": 0.01, "top_fraction": 1.0})
    picks = [(v, t) for t, v in _evaluate(s, lake).items() if v is not None]
    prices = _prices(lake)
    orders = s.decide(picks, Portfolio(cash=10_000.0, positions={}), prices, WED)
    assert orders
    assert sum(o.quantity * prices[o.ticker] for o in orders) <= 10_000.0 + 1e-6


def test_index_filter_blocks_new_buys_but_keeps_holdings(bear_lake):
    s = StocksOnTheMove({"top_fraction": 1.0})
    picks = [(v, t) for t, v in _evaluate(s, bear_lake).items() if v is not None]
    held = {"S5.US": 10.0, "DOWN.US": 10.0}
    prices = _prices(bear_lake)
    orders = s.decide(picks, Portfolio(cash=50_000.0, positions=held), prices, WED)
    assert ("DOWN.US", "sell") in {(o.ticker, o.side) for o in orders}
    # held S5 may be resized; nothing new is bought
    assert {o.ticker for o in orders if o.side == "buy"} <= {"S5.US"}
    assert len(picks) > 2


def test_index_filter_can_be_disabled(bear_lake):
    s = StocksOnTheMove({"index_ticker": ""})
    picks = [(v, t) for t, v in _evaluate(s, bear_lake).items() if v is not None]
    orders = s.decide(picks, Portfolio(cash=100_000.0, positions={}), _prices(bear_lake), WED)
    assert any(o.side == "buy" for o in orders)


def test_missing_index_history_blocks_new_buys(tmp_path):
    lake = _lake(tmp_path / "noindex.duckdb", _series())
    try:
        s = StocksOnTheMove({})
        picks = [(v, t) for t, v in _evaluate(s, lake).items() if v is not None]
        assert picks
        assert s.decide(picks, Portfolio(cash=100_000.0, positions={}), _prices(lake), WED) == []
    finally:
        lake.close()


def test_a_fresh_instance_sells_but_never_buys_blind(lake):
    # production decides on a freshly loaded instance: no ATRs, no index read
    picks = [(v, t) for t, v in _evaluate(StocksOnTheMove({}), lake).items() if v is not None]
    held = {"DOWN.US": 5.0}
    orders = StocksOnTheMove({}).decide(
        picks, Portfolio(cash=100_000.0, positions=held), _prices(lake), WED
    )
    assert {(o.ticker, o.side) for o in orders} == {("DOWN.US", "sell")}


# --- persistence and backtest -----------------------------------------------------


def test_save_load_round_trip(tmp_path):
    s = StocksOnTheMove({"lookback": 120, "index_ticker": "QQQ.US", "universe": "A.US"})
    s.save(tmp_path / "sotm")
    assert StocksOnTheMove.load(tmp_path / "sotm").params == s.params


def test_small_backtest(lake):
    broker = SimulatedBroker(Portfolio(cash=100_000.0), slippage_bps=0.0, fee_per_trade=0.0)
    config = BacktestConfig(start=date(2024, 5, 1), end=date(2024, 7, 31), universe=STOCKS)
    report = Backtester([StocksOnTheMove({})], broker, lake, config).run()
    book = broker.fetch_portfolio()
    held = {t for t, q in book.positions.items() if q > 0}
    assert held and held <= {f"S{i}.US" for i in range(6)} | {"GAP.US", "BELOW.US"}
    assert book.cash >= -1e-6
    assert len(report.equity_curve) > 50
