"""PairsReversion: long the cheap leg, short the rich one (roadmap 16.3)."""

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
from stonks.strategies.examples.pairs_reversion import PairsReversion, parse_pairs
from tests.unit.trend_helpers import DATES, LAST, build_lake

N = len(DATES)
SPIKE = 5


def _series() -> dict[str, np.ndarray]:
    rng = np.random.default_rng(7)
    b = 40.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, N)))
    noise = rng.normal(0.0, 0.005, N)
    noise[-SPIKE:] = 0.02  # A trades 4 sd rich for the last few bars
    a = np.exp(np.log(b) + 0.2 + noise)
    return {"A.US": a, "B.US": b, "C.US": 30.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, N)))}


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    db = build_lake(tmp_path_factory.mktemp("pairs") / "lake.duckdb", _series())
    yield db
    db.close()


def test_catalogued_with_metadata():
    assert strategy_catalog()["pairs_reversion"] is PairsReversion
    meta = strategy_metadata(PairsReversion)
    assert meta.alpha_family == "reversion"
    assert meta.premise == "mean_reversion"
    assert meta.hypothesis
    assert isinstance(PairsReversion({}), Strategy)
    assert PairsReversion({}).supports_short is False


def test_parse_pairs():
    assert parse_pairs(" A:B , C:D,, A:B") == [("A", "B"), ("C", "D")]
    with pytest.raises(ValueError):
        parse_pairs("A")
    with pytest.raises(ValueError):
        parse_pairs("A:A")


def test_bands_must_be_ordered():
    with pytest.raises(ValueError):
        PairsReversion({"entry_z": 2.0, "exit_z": 2.0})


def test_rich_leg_is_short_and_cheap_leg_long(lake):
    s = PairsReversion({"pairs": "A.US:B.US", "short_mode": "short"})
    state, z = s.pair_view("A.US", "B.US", LAST, lake)
    assert state == -1 and z > 2.0
    assert s.estimate_return("A.US", LAST, lake) == pytest.approx(-z)
    assert s.estimate_return("B.US", LAST, lake) == pytest.approx(z)


def test_long_only_keeps_just_the_long_leg(lake):
    s = PairsReversion({"pairs": "A.US:B.US"})
    assert s.estimate_return("A.US", LAST, lake) is None
    assert s.estimate_return("B.US", LAST, lake) > 0


def test_unrelated_pair_does_not_trade(lake):
    s = PairsReversion({"pairs": "A.US:C.US", "short_mode": "short", "max_half_life": 5})
    assert s.pair_view("A.US", "C.US", LAST, lake) is None
    assert s.estimate_return("A.US", LAST, lake) is None


def test_no_pairs_never_trades(lake):
    assert PairsReversion({}).estimate_return("A.US", LAST, lake) is None


def test_short_book_holds_a_dollar_neutral_pair(lake):
    broker = SimulatedBroker(
        Portfolio(cash=100_000.0), margin=MarginSettings(model="reg_t").build(), allow_short=True
    )
    config = BacktestConfig(
        start=DATES[-SPIKE - 2].date(),
        end=LAST.date(),
        universe=["A.US", "B.US"],
        allow_short=True,
    )
    strategy = PairsReversion({"pairs": "A.US:B.US", "short_mode": "short"})
    Backtester([strategy], broker, lake, config).run()
    book = broker.fetch_portfolio()
    prices = {t: float(_series()[t][-1]) for t in ("A.US", "B.US")}
    short_value = -book.positions["A.US"] * prices["A.US"]
    long_value = book.positions["B.US"] * prices["B.US"]
    assert short_value > 0 and long_value > 0
    # each leg is allocation / 2 = 0.5 of equity when it filled
    assert short_value == pytest.approx(long_value, rel=0.1)
