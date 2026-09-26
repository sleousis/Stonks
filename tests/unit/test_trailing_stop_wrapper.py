"""TrailingStopWrapper: an ATR trailing stop around any inner strategy,
rebuilt from bars on every call."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.protocols import Strategy, SurvivalReport
from stonks.core.types import Portfolio
from stonks.features.trailing_stop import Trade
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.ewmac_trend import EWMACTrend
from stonks.strategies.trailing_stop import TrailingStopWrapper
from tests.unit.nt888_helpers import SCRIPTED, STUB, FittedStub, make_lake, write_bars

BUY_AND_HOLD = "stonks.strategies.examples.buy_and_hold:BuyAndHold"
EWMAC = "stonks.strategies.examples.ewmac_trend:EWMACTrend"
DATES = pd.bdate_range("2022-01-03", periods=300)
CRASH = 200


def _closes() -> np.ndarray:
    closes = 50.0 * np.exp(0.005 * np.arange(len(DATES)))
    closes[CRASH:] *= 0.8  # a one-day 20% drop, then the climb resumes
    return closes


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    db = make_lake(tmp_path_factory.mktemp("tsw") / "lake.duckdb")
    write_bars(db, "X.US", DATES, _closes())
    yield db
    db.close()


def _at(i: int):
    return DATES[i].to_pydatetime()


def _wrapper(**overrides) -> TrailingStopWrapper:
    params = {
        "inner_class_path": BUY_AND_HOLD,
        "inner_params": {"ticker": "X.US"},
        **overrides,
    }
    return TrailingStopWrapper(params)


# ---- surface -------------------------------------------------------------------------


def test_satisfies_the_protocol_and_mirrors_the_inner_strategy():
    w = _wrapper(inner_class_path=EWMAC, inner_params={})
    assert isinstance(w, Strategy)
    assert w.id == "ewmac_trend_trailing_stop"
    assert w.applicable_asset_classes == ("equity", "crypto", "commodity")
    assert isinstance(w.inner, EWMACTrend)


def test_parameter_spec_defaults():
    p = _wrapper().params
    assert (p["k_atr"], p["atr_period"], p["cooldown_bars"]) == (3.0, 20, 5)
    assert p["replay_bars"] == 500
    assert p["inner_params"] == {"ticker": "X.US", "allocation": 1.0}


# ---- stop logic ----------------------------------------------------------------------


def test_stop_exits_on_the_crash_bar_and_reenters_after_the_cooldown(lake):
    trades = _wrapper().trade_log("X.US", _at(len(DATES) - 1), lake)
    # the inner is long from the first replayed bar; stopped on the crash bar,
    # bars 201..205 cool down, back in on bar 206
    assert trades == [Trade(0, CRASH, "stop"), Trade(CRASH + 6, None, None)]


def test_estimate_return_is_blocked_while_stopped_out(lake):
    w = _wrapper()
    assert w.estimate_return("X.US", _at(CRASH - 1), lake) == 1.0
    for i in range(CRASH, CRASH + 6):
        assert w.estimate_return("X.US", _at(i), lake) is None
    assert w.estimate_return("X.US", _at(CRASH + 6), lake) == 1.0
    assert w.estimate_return("OTHER.US", _at(CRASH - 1), lake) is None  # inner says no


def test_a_wider_stop_rides_through_the_crash(lake):
    assert _wrapper(k_atr=12.0).trade_log("X.US", _at(250), lake) == [Trade(0, None, None)]


def test_zero_cooldown_reenters_on_the_next_bar(lake):
    trades = _wrapper(cooldown_bars=0).trade_log("X.US", _at(250), lake)
    assert trades[1].entry == CRASH + 1


def test_the_stop_never_reads_bars_after_as_of(tmp_path, lake):
    changed = _closes()
    changed[CRASH - 1 :] *= 0.1  # crash a bar earlier in the "future" lake
    other = make_lake(tmp_path / "future.duckdb")
    try:
        write_bars(other, "X.US", DATES, changed)
        w = _wrapper()
        for i in (CRASH - 10, CRASH - 2):
            assert w.trade_log("X.US", _at(i), lake) == w.trade_log("X.US", _at(i), other)
            assert w.estimate_return("X.US", _at(i), lake) == w.estimate_return(
                "X.US", _at(i), other
            )
    finally:
        other.close()


def test_the_inner_exit_ends_the_trade_and_signals_are_memoised(tmp_path):
    db = make_lake(tmp_path / "scripted.duckdb")
    closes = 50.0 * np.exp(0.002 * np.arange(60))
    volumes = np.where((np.arange(60) >= 10) & (np.arange(60) < 30), 3e6, 1e6)
    volumes[40:] = 3e6
    write_bars(db, "X.US", DATES[:60], closes, volumes)
    try:
        w = _wrapper(inner_class_path=SCRIPTED, inner_params={"ticker": "X.US"})
        assert w.trade_log("X.US", _at(59), db) == [Trade(10, 30, "signal"), Trade(40, None, None)]
        calls = w.inner.calls
        w.trade_log("X.US", _at(59), db)
        assert w.inner.calls == calls  # every bar's inner signal memoised
    finally:
        db.close()


def test_replay_window_is_bounded(lake):
    trades = _wrapper(replay_bars=50).trade_log("X.US", _at(CRASH + 20), lake)
    # the window starts at bar 171: indices are relative to it
    assert trades[0] == Trade(0, CRASH - 171, "stop")


# ---- decide --------------------------------------------------------------------------


def test_decide_sells_a_stopped_position_and_drops_inner_buys(lake):
    w = _wrapper()
    w.estimate_return("X.US", _at(CRASH), lake)  # remembers the lake
    held = Portfolio(cash=0.0, positions={"X.US": 5.0})
    orders = w.decide([], held, {"X.US": 10.0}, _at(CRASH))
    assert [(o.side, o.ticker, o.quantity) for o in orders] == [("sell", "X.US", 5.0)]
    flat = w.decide([(1.0, "X.US")], Portfolio(cash=1_000.0), {"X.US": 10.0}, _at(CRASH + 2))
    assert flat == []


def test_decide_delegates_while_the_stop_holds(lake):
    w = _wrapper()
    w.estimate_return("X.US", _at(CRASH - 1), lake)
    orders = w.decide([(1.0, "X.US")], Portfolio(cash=1_000.0), {"X.US": 10.0}, _at(CRASH - 1))
    assert [(o.side, o.ticker) for o in orders] == [("buy", "X.US")]


def test_decide_without_a_lake_is_transparent():
    w = _wrapper()
    orders = w.decide([(1.0, "X.US")], Portfolio(cash=1_000.0), {"X.US": 10.0}, _at(CRASH))
    assert [(o.side, o.ticker) for o in orders] == [("buy", "X.US")]


def test_wrapping_ewmac_cuts_the_trend_that_ewmac_still_holds(tmp_path):
    db = make_lake(tmp_path / "ewmac.duckdb")
    dates = pd.bdate_range("2020-01-02", periods=600)
    rng = np.random.default_rng(5)
    closes = 50.0 * np.exp(np.cumsum(0.003 + rng.normal(0.0, 0.01, 600)))
    closes[590:] *= 0.85
    write_bars(db, "X.US", dates, closes)
    try:
        inner = EWMACTrend({})
        w = TrailingStopWrapper({"inner_class_path": EWMAC, "inner_params": {}, "replay_bars": 120})
        as_of = dates[590].to_pydatetime()
        assert inner.estimate_return("X.US", as_of, db) is not None
        assert w.estimate_return("X.US", as_of, db) is None
    finally:
        db.close()


def test_small_backtest_of_a_wrapped_trend_strategy_stops_out_after_the_crash(lake):
    broker = SimulatedBroker(Portfolio(cash=100_000.0), slippage_bps=0.0, fee_per_trade=0.0)
    w = _wrapper(
        inner_class_path="stonks.strategies.examples.tsmom:TimeSeriesMomentum",
        inner_params={"lookbacks": "125", "rebalance": "daily"},
        cooldown_bars=60,
    )
    config = BacktestConfig(
        start=DATES[180].date(), end=DATES[CRASH + 30].date(), universe=["X.US"]
    )
    Backtester([w], broker, lake, config).run()
    days = [pd.Timestamp(f.filled_at).date() for f in broker.fills]
    assert days[0] == DATES[181].date()  # in from the first decision
    assert days[-1] == DATES[CRASH + 1].date()  # out the session after the crash
    assert broker.fetch_portfolio().positions.get("X.US", 0.0) == 0.0
    assert broker.fetch_portfolio().cash > 0


# ---- persistence ---------------------------------------------------------------------


def test_save_load_round_trip(tmp_path, lake):
    w = _wrapper(inner_class_path=STUB, inner_params={"ticker": "X.US"}, k_atr=2.5)
    w.fit(None)
    w.save(tmp_path / "art")
    loaded = TrailingStopWrapper.load(tmp_path / "art")
    assert loaded.params == w.params
    assert isinstance(loaded.inner, FittedStub)
    assert loaded.inner.fitted_value == 42.0


def test_registry_round_trip(tmp_path, lake):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    try:
        registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
        w = _wrapper(inner_class_path=STUB, inner_params={"ticker": "X.US"})
        w.fit(None)
        sid = registry.register(w, [SurvivalReport(test_id="oos", passed=True, metrics={})])
        loaded = registry.load(sid)
        assert isinstance(loaded, TrailingStopWrapper)
        assert loaded.inner.fitted_value == 42.0
        assert loaded.estimate_return("X.US", _at(CRASH - 1), lake) == 1.0
        assert loaded.estimate_return("X.US", _at(CRASH), lake) is None
    finally:
        state.close()
