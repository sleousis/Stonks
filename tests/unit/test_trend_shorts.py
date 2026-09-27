"""EWMAC and TSMOM short their down trends when allowed (roadmap 16.3)."""

from __future__ import annotations

import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Portfolio
from stonks.execution.margin import MarginSettings
from stonks.strategies.examples.ewmac_trend import EWMACTrend
from stonks.strategies.examples.tsmom import TimeSeriesMomentum
from tests.unit.trend_helpers import DATES, LAST, build_lake, trend

SERIES = {"UP.US": trend(0.003, seed=1), "DOWN.US": trend(-0.003, seed=2)}


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    db = build_lake(tmp_path_factory.mktemp("trend_shorts") / "lake.duckdb", SERIES)
    yield db
    db.close()


def test_short_mode_is_off_by_default():
    for cls in (TimeSeriesMomentum, EWMACTrend):
        s = cls({})
        assert s.short_capable is True
        assert s.supports_short is False
        assert "short_mode" not in s.params


def test_short_mode_returns_the_negative_forecast(lake):
    flat = TimeSeriesMomentum({"rebalance": "daily"})
    short = TimeSeriesMomentum({"rebalance": "daily", "short_mode": "short"})
    assert flat.estimate_return("DOWN.US", LAST, lake) is None
    assert short.estimate_return("DOWN.US", LAST, lake) == pytest.approx(-11.0)
    assert short.estimate_return("UP.US", LAST, lake) == pytest.approx(11.0)


def test_ewmac_short_mode_goes_negative_in_a_down_trend(lake):
    short = EWMACTrend({"short_mode": "short"})
    r = short.estimate_return("DOWN.US", LAST, lake)
    assert r is not None and r < 0
    assert EWMACTrend({}).estimate_return("DOWN.US", LAST, lake) is None


def _run(lake, strategy, allow_short: bool) -> Portfolio:
    margin = MarginSettings(model="reg_t").build() if allow_short else None
    broker = SimulatedBroker(Portfolio(cash=100_000.0), margin=margin, allow_short=allow_short)
    config = BacktestConfig(
        start=DATES[450].date(),
        end=LAST.date(),
        universe=["UP.US", "DOWN.US"],
        allow_short=allow_short,
    )
    Backtester([strategy], broker, lake, config).run()
    return broker.fetch_portfolio()


def test_short_book_holds_the_down_trend_short(lake):
    book = _run(lake, TimeSeriesMomentum({"short_mode": "short"}), allow_short=True)
    assert book.positions.get("UP.US", 0.0) > 0
    assert book.positions.get("DOWN.US", 0.0) < 0


def test_long_only_book_drops_the_short_legs(lake):
    book = _run(lake, TimeSeriesMomentum({"short_mode": "short"}), allow_short=False)
    assert book.positions.get("UP.US", 0.0) > 0
    assert book.positions.get("DOWN.US", 0.0) == 0.0
