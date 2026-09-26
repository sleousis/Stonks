"""Short Backtester runs for the neurotrader888 chart-pattern strategies on
synthetic bars that contain one clean pattern each: every strategy enters
once (fills at the next bar's open) and is flat again by the end."""

from __future__ import annotations

import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.interval import Interval
from stonks.core.types import Portfolio
from stonks.strategies.examples.flag_pennant import FlagPennantStrategy
from stonks.strategies.examples.harmonic_xabcd import HarmonicXABCDStrategy
from stonks.strategies.examples.head_shoulders import HeadShouldersStrategy
from stonks.strategies.examples.market_structure_break import MarketStructureBreakStrategy
from tests.nt888_synth import as_of, bars_frame, knots_path, make_lake
from tests.unit.test_flag_pennant import FLAG_KNOTS
from tests.unit.test_harmonic_xabcd import GARTLEY_KNOTS
from tests.unit.test_head_shoulders import IHS_KNOTS
from tests.unit.test_market_structure_break import STAIRS

CASES = [
    pytest.param(HeadShouldersStrategy, {"order": 3}, IHS_KNOTS, id="head_shoulders"),
    pytest.param(FlagPennantStrategy, {"order": 8, "variant": "pips"}, FLAG_KNOTS, id="flag_pips"),
    pytest.param(
        FlagPennantStrategy, {"order": 8, "variant": "trendline"}, FLAG_KNOTS, id="flag_trendline"
    ),
    pytest.param(HarmonicXABCDStrategy, {"sigma": 0.03}, GARTLEY_KNOTS, id="harmonic"),
    pytest.param(MarketStructureBreakStrategy, {"atr_lookback": 14, "level": 0}, STAIRS, id="msb"),
]


@pytest.mark.parametrize(("cls", "params", "knots"), CASES)
def test_backtest_enters_and_exits_one_pattern(tmp_path, cls, params, knots):
    closes = knots_path(knots)
    lake = make_lake(tmp_path, bars_frame(closes))
    try:
        broker = SimulatedBroker(portfolio=Portfolio(cash=10_000.0, positions={}))
        strategy = cls({"ticker": "X.US", "interval": "1d", **params})
        config = BacktestConfig(
            start=as_of(0),
            end=as_of(len(closes) - 1),
            universe=["X.US"],
            interval=Interval.DAY_1,
        )
        report = Backtester([strategy], broker, lake, config).run()
    finally:
        lake.close()

    fills = broker.reconcile()
    buys = [f for f in fills if f.side == "buy"]
    sells = [f for f in fills if f.side == "sell"]
    assert buys, "strategy never entered"
    assert sells, "strategy never exited"
    assert len(report.equity_curve) == len(closes)
    assert broker.fetch_portfolio().positions.get("X.US", 0.0) == pytest.approx(0.0)
    assert report.final_return != 0.0
