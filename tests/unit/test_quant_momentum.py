"""QuantMomentum (Gray & Vogel): 12-2 momentum, top decile, frog-in-the-pan
filter, equal weight, quarterly rebalance on the last session of Feb, May,
Aug and Nov."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Portfolio
from stonks.lab.catalog import strategy_catalog
from stonks.store.lake import DuckDBLake
from stonks.strategies.base import strategy_metadata
from stonks.strategies.examples.quant_momentum import QuantMomentum, select_quant_momentum

DATES = pd.bdate_range("2022-12-01", "2024-03-15")
REBALANCE = date(2024, 2, 29)
UNIVERSE = [f"T{i:02d}.US" for i in range(20)]


def _returns(i: int, n: int) -> np.ndarray:
    if i == 18:  # the biggest winner, made of a few jumps
        rets = np.full(n, -0.0005)
        rets[::25] = 0.08
        return rets
    return np.full(n, 0.0001 * i if i < 19 else 0.002)  # smooth, T19 second best


def _frame(ticker: str, closes: np.ndarray, dates=DATES) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": ticker,
            "date": [d.date() for d in dates],
            "open": closes,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "adj_close": closes,
            "volume": 1_000_000,
        }
    )


def _closes(i: int, n: int = len(DATES)) -> np.ndarray:
    return 100.0 * np.cumprod(1 + _returns(i, n))


def _lake(path, frames) -> DuckDBLake:
    lake = DuckDBLake(path)
    lake.migrate()
    lake.upsert_prices(pd.concat(frames, ignore_index=True))
    return lake


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    lake = _lake(
        tmp_path_factory.mktemp("qm") / "lake.duckdb",
        [_frame(t, _closes(i)) for i, t in enumerate(UNIVERSE)],
    )
    yield lake
    lake.close()


def _held(orders, side):
    return {o.ticker for o in orders if o.side == side}


# --- catalog and metadata ---------------------------------------------------


def test_catalogued_with_metadata():
    assert strategy_catalog()["quant_momentum"] is QuantMomentum
    meta = strategy_metadata(QuantMomentum)
    assert meta.alpha_family == "trend"
    assert meta.premise == "trend"
    assert meta.label_horizon_bars == 63
    assert meta.required_history_bars == 253
    assert meta.applicable_asset_classes == ("equity",)
    assert "underreact" in meta.hypothesis


def test_skip_must_be_shorter_than_formation():
    with pytest.raises(ValueError, match="skip_bars"):
        QuantMomentum({"formation_bars": 126, "skip_bars": 126})


# --- selection ----------------------------------------------------------------


def test_select_keeps_top_decile_then_the_most_continuous_half():
    stats = {f"X{i:02d}": (0.01 * i, 0.0) for i in range(20)}
    stats["X19"] = (0.19, 0.5)  # best, but jumpy
    stats["X18"] = (0.18, -0.9)
    assert select_quant_momentum(stats, top_pct=0.1, id_keep_pct=0.5) == ["X18"]
    assert select_quant_momentum(stats, top_pct=0.1, id_keep_pct=1.0) == ["X18", "X19"]


def test_select_rounds_up_and_keeps_at_least_one():
    stats = {f"X{i:02d}": (0.01 * i, -0.5) for i in range(30)}
    # 10% of 30 is 3 (not 4 from float noise); half of 3 rounds up to 2
    assert len(select_quant_momentum(stats, 0.1, 0.5)) == 2
    assert select_quant_momentum({"A": (0.1, 0.0)}, 0.1, 0.5) == ["A"]
    assert select_quant_momentum({}, 0.1, 0.5) == []


def test_absolute_momentum_drops_losing_winners():
    stats = {"A": (-0.1, -1.0), "B": (-0.2, -1.0)}
    assert select_quant_momentum(stats, 0.5, 1.0) == ["A"]
    assert select_quant_momentum(stats, 0.5, 1.0, absolute_momentum=True) == []


def test_score_universe_picks_the_smooth_winner(lake):
    s = QuantMomentum({})
    assert s.score_universe(UNIVERSE, REBALANCE, lake) == {"T19.US": 1.0}
    both = QuantMomentum({"id_keep_pct": 1.0}).score_universe(UNIVERSE, REBALANCE, lake)
    assert both == {"T18.US": 1.0, "T19.US": 1.0}


def test_estimate_return_answers_from_the_lake_universe(lake):
    s = QuantMomentum({})
    got = {t: s.estimate_return(t, REBALANCE, lake) for t in reversed(UNIVERSE)}
    assert got["T19.US"] == 1.0
    assert all(v is None for t, v in got.items() if t != "T19.US")


def test_universe_param_restricts_the_cross_section(lake):
    s = QuantMomentum({"universe": "T00.US,T01.US,T02.US"})
    assert s.estimate_return("T02.US", REBALANCE, lake) == 1.0
    assert s.estimate_return("T19.US", REBALANCE, lake) is None


# --- look-ahead and survivorship ----------------------------------------------


def _single(tmp_path, name, closes, dates=DATES):
    return _lake(tmp_path / f"{name}.duckdb", [_frame("A.US", closes, dates)])


def test_the_skip_month_boundary(tmp_path):
    base = np.linspace(100.0, 150.0, len(DATES))
    t = int(np.searchsorted(DATES, pd.Timestamp(REBALANCE)))
    s = QuantMomentum({})

    def r_of(closes, name):
        lake = _single(tmp_path, name, closes)
        try:
            return s._stats("A.US", REBALANCE, lake)[0]
        finally:
            lake.close()

    r0 = r_of(base, "base")
    assert r0 == pytest.approx(base[t - 21] / base[t - 252] - 1)
    inside_skip = base.copy()
    inside_skip[t - 20 : t + 1] *= 3.0
    assert r_of(inside_skip, "inside") == pytest.approx(r0)
    at_boundary = base.copy()
    at_boundary[t - 21] *= 3.0
    assert r_of(at_boundary, "boundary") != pytest.approx(r0)


def test_future_bars_never_change_a_decision(tmp_path, lake):
    frames = []
    for i, t in enumerate(UNIVERSE):
        closes = _closes(i)
        after = pd.Timestamp(REBALANCE) < DATES
        closes[after] = closes[after] * (10.0 if i == 0 else 0.1)
        frames.append(_frame(t, closes))
    other = _lake(tmp_path / "future.duckdb", frames)
    try:
        s = QuantMomentum({})
        assert s.score_universe(UNIVERSE, REBALANCE, other) == s.score_universe(
            UNIVERSE, REBALANCE, lake
        )
    finally:
        other.close()


def test_a_ticker_listed_mid_window_is_not_ranked(tmp_path):
    frames = [_frame(t, _closes(i)) for i, t in enumerate(UNIVERSE)]
    ipo_dates = DATES[-200:]
    frames.append(_frame("NEW.US", 10.0 * 1.01 ** np.arange(len(ipo_dates)), ipo_dates))
    lake = _lake(tmp_path / "ipo.duckdb", frames)
    try:
        s = QuantMomentum({})
        assert "NEW.US" not in s.score_universe([*UNIVERSE, "NEW.US"], REBALANCE, lake)
        assert s.estimate_return("NEW.US", REBALANCE, lake) is None
    finally:
        lake.close()


def test_a_delisted_ticker_is_not_ranked_on_frozen_history(tmp_path):
    dead_dates = DATES[pd.Timestamp("2024-01-10") >= DATES]
    frames = [_frame(t, _closes(i)) for i, t in enumerate(UNIVERSE)]
    frames.append(_frame("DEAD.US", 10.0 * 1.01 ** np.arange(len(dead_dates)), dead_dates))
    lake = _lake(tmp_path / "dead.duckdb", frames)
    try:
        got = QuantMomentum({}).score_universe([*UNIVERSE, "DEAD.US"], REBALANCE, lake)
        assert "DEAD.US" not in got
    finally:
        lake.close()


# --- decide -------------------------------------------------------------------


def test_decide_only_trades_on_the_rebalance_day():
    s = QuantMomentum({})
    picks = [(1.0, "A"), (1.0, "B")]
    prices = {"A": 10.0, "B": 20.0, "C": 5.0}
    book = Portfolio(cash=10_000.0, positions={"C": 100.0})
    assert s.decide(picks, book, prices, date(2024, 2, 28)) == []
    assert s.decide(picks, book, prices, date(2024, 3, 28)) == []
    orders = s.decide(picks, book, prices, REBALANCE)
    assert _held(orders, "sell") == {"C"}
    buys = {o.ticker: o.quantity * prices[o.ticker] for o in orders if o.side == "buy"}
    assert buys == {"A": pytest.approx(5_250.0), "B": pytest.approx(5_250.0)}


def test_decide_never_spends_more_than_the_book():
    s = QuantMomentum({})
    picks = [(1.0, t) for t in "ABCD"]
    prices = dict.fromkeys("ABCD", 10.0)
    orders = s.decide(picks, Portfolio(cash=1_000.0, positions={}), prices, REBALANCE)
    assert sum(o.quantity * prices[o.ticker] for o in orders) <= 1_000.0 + 1e-6


def test_decide_with_no_picks_exits_on_the_rebalance_day():
    s = QuantMomentum({})
    orders = s.decide([], Portfolio(cash=0.0, positions={"A": 5.0}), {"A": 10.0}, REBALANCE)
    assert [(o.ticker, o.side, o.quantity) for o in orders] == [("A", "sell", 5.0)]


# --- persistence and backtest ---------------------------------------------------


def test_save_load_round_trip(tmp_path):
    s = QuantMomentum({"top_pct": 0.2, "universe": "A.US,B.US", "absolute_momentum": True})
    s.save(tmp_path / "qm")
    loaded = QuantMomentum.load(tmp_path / "qm")
    assert loaded.params == s.params


def test_small_backtest_buys_the_selection_after_the_rebalance(lake):
    broker = SimulatedBroker(Portfolio(cash=100_000.0), slippage_bps=0.0, fee_per_trade=0.0)
    config = BacktestConfig(start=date(2024, 2, 1), end=date(2024, 3, 15), universe=UNIVERSE)
    report = Backtester([QuantMomentum({})], broker, lake, config).run()
    positions = broker.fetch_portfolio().positions
    assert {t for t, q in positions.items() if q > 0} == {"T19.US"}
    assert len(report.equity_curve) > 20
