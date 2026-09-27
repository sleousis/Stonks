"""LongShortMomentum: long past winners, short past losers (roadmap 16.3)."""

from __future__ import annotations

import numpy as np
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.protocols import Strategy
from stonks.core.types import Portfolio
from stonks.execution.margin import MarginSettings
from stonks.lab.catalog import strategy_catalog
from stonks.strategies.base import strategy_metadata
from stonks.strategies.examples.ls_momentum import LongShortMomentum, select_legs
from tests.unit.trend_helpers import DATES, LAST, build_lake

N = len(DATES)
DRIFTS = {"W1.US": 0.004, "W2.US": 0.002, "M.US": 0.0, "L2.US": -0.002, "L1.US": -0.004}
SERIES = {t: 50.0 * np.exp(np.cumsum(np.full(N, d))) for t, d in DRIFTS.items()}
UNIVERSE = ",".join(DRIFTS)


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    db = build_lake(tmp_path_factory.mktemp("lsmom") / "lake.duckdb", SERIES)
    yield db
    db.close()


def _r(ticker: str) -> float:
    closes = SERIES[ticker]
    return closes[-22] / closes[-253] - 1.0


def test_catalogued_with_metadata():
    assert strategy_catalog()["ls_momentum"] is LongShortMomentum
    meta = strategy_metadata(LongShortMomentum)
    assert meta.alpha_family == "trend"
    assert meta.hypothesis
    assert isinstance(LongShortMomentum({}), Strategy)
    assert LongShortMomentum({}).supports_short is False


def test_select_legs():
    returns = {"A": 0.3, "B": 0.1, "C": 0.0, "D": -0.2}
    assert select_legs(returns, 0.25) == (["A"], ["D"])
    assert select_legs(returns, 0.5) == (["A", "B"], ["D", "C"])
    # one name is a winner, never a loser too
    assert select_legs({"A": 0.1}, 0.5) == (["A"], [])
    assert select_legs({}, 0.5) == ([], [])


def test_scores_are_demeaned_momentum(lake):
    s = LongShortMomentum({"universe": UNIVERSE, "short_mode": "short"})
    median = _r("M.US")
    assert s.estimate_return("W1.US", LAST, lake) == pytest.approx(_r("W1.US") - median)
    assert s.estimate_return("L1.US", LAST, lake) == pytest.approx(_r("L1.US") - median)
    assert s.estimate_return("M.US", LAST, lake) is None


def test_long_only_holds_just_the_winners(lake):
    s = LongShortMomentum({"universe": UNIVERSE})
    assert s.estimate_return("W1.US", LAST, lake) > 0
    assert s.estimate_return("L1.US", LAST, lake) is None


def test_short_book_is_a_dollar_neutral_spread(lake):
    broker = SimulatedBroker(
        Portfolio(cash=100_000.0), margin=MarginSettings(model="reg_t").build(), allow_short=True
    )
    config = BacktestConfig(
        start=DATES[-10].date(),  # includes the last session of August
        end=LAST.date(),
        universe=list(DRIFTS),
        allow_short=True,
    )
    strategy = LongShortMomentum({"universe": UNIVERSE, "short_mode": "short"})
    report = Backtester([strategy], broker, lake, config).run()
    book = broker.fetch_portfolio()
    assert set(book.positions) == {"W1.US", "L1.US"}
    long_value = book.positions["W1.US"] * SERIES["W1.US"][-1]
    short_value = -book.positions["L1.US"] * SERIES["L1.US"][-1]
    # each leg took half of the equity when it filled
    assert long_value == pytest.approx(0.5 * report.equity_curve[-1], rel=0.05)
    assert short_value == pytest.approx(0.5 * report.equity_curve[-1], rel=0.05)
