"""FactorStrategy (roadmap 22.2): hold the top slice of a universe ranked by
any factor, library id or formula, rebalanced on month ends."""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from stonks.core.types import Portfolio
from stonks.factors.engine import latest_values
from stonks.factors.expression import parse_factor
from stonks.lab.catalog import strategy_catalog
from stonks.store.lake import DuckDBLake
from stonks.strategies.base import strategy_metadata
from stonks.strategies.examples.factor_strategy import FactorStrategy

DATES = pd.bdate_range("2023-01-02", "2024-03-15")
UNIVERSE = [f"F{i:02d}.US" for i in range(10)]


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


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    rng = np.random.default_rng(11)
    frames = []
    for i, ticker in enumerate(UNIVERSE):
        # drift rises with i, noise falls with i: F09 wins momentum and is calmest
        rets = rng.normal(0.0002 * i, 0.03 - 0.0025 * i, len(DATES))
        frames.append(_frame(ticker, 100.0 * np.exp(np.cumsum(rets))))
    db = DuckDBLake(tmp_path_factory.mktemp("fs") / "lake.duckdb")
    db.migrate()
    db.upsert_prices(pd.concat(frames, ignore_index=True))
    yield db
    db.close()


def _strategy(**params) -> FactorStrategy:
    return FactorStrategy({"universe": ",".join(UNIVERSE)} | params)


def test_catalogued_with_metadata():
    assert strategy_catalog()["factor"] is FactorStrategy
    strategy = _strategy()
    meta = strategy_metadata(strategy)
    assert meta.alpha_family == "data_driven"
    # mom_12_1 reads 252 bars back
    assert strategy.required_history_bars == 253
    fundamentals = _strategy(factor="piotroski_f")
    assert strategy_metadata(fundamentals).applicable_asset_classes == ("equity",)


def test_bad_factor_is_refused():
    with pytest.raises(ValueError, match="factor"):
        _strategy(factor="Ref($close, -1)")
    with pytest.raises(ValueError, match="top_pct"):
        _strategy(top_pct=0.0)


def test_picks_the_top_slice_in_the_factor_direction(lake):
    as_of = datetime(2024, 2, 29)
    momentum = _strategy(factor="mom_6_1", top_pct=0.2)
    picks = {t: momentum.estimate_return(t, as_of, lake) for t in UNIVERSE}
    held = {t for t, v in picks.items() if v is not None}
    assert len(held) == 2
    values = latest_values(
        parse_factor("Ref($close, 21)/Ref($close, 126)-1"), lake, UNIVERSE, as_of
    )
    best = sorted(values, key=values.get, reverse=True)[:2]
    assert held == set(best)
    # low_vol_60 has direction -1: the calmest names are held
    calm = _strategy(factor="low_vol_60", top_pct=0.2)
    held = {t for t in UNIVERSE if calm.estimate_return(t, as_of, lake) is not None}
    assert held == {"F08.US", "F09.US"}


def test_a_formula_is_a_factor(lake):
    strategy = _strategy(factor="$close / Ref($close, 20) - 1", top_pct=0.3)
    as_of = datetime(2024, 2, 29)
    scores = [strategy.estimate_return(t, as_of, lake) for t in UNIVERSE]
    kept = [s for s in scores if s is not None]
    assert len(kept) == 3
    assert all(0 < s <= 1 for s in kept)
    assert strategy.required_history_bars == 21


def test_decide_rebalances_on_month_ends_only(lake):
    strategy = _strategy(factor="mom_6_1", top_pct=0.2)
    portfolio = Portfolio(cash=100_000.0, positions={})
    prices = dict.fromkeys(UNIVERSE, 100.0)
    picks = [(0.9, "F09.US"), (0.8, "F08.US")]
    assert strategy.decide(picks, portfolio, prices, date(2024, 2, 28)) == []
    orders = strategy.decide(picks, portfolio, prices, date(2024, 2, 29))
    assert {o.ticker for o in orders} == {"F09.US", "F08.US"}
    assert all(o.side == "buy" for o in orders)


def test_latest_values_hide_the_open_daily_bar(lake):
    """An intraday decision sees only closed daily bars (RS-03)."""
    node = parse_factor("$close / Ref($close, 1) - 1")
    day = datetime(2024, 2, 29)
    prev = datetime(2024, 2, 28)
    intraday = day.replace(hour=9, minute=30)
    assert latest_values(node, lake, UNIVERSE, intraday) == latest_values(
        node, lake, UNIVERSE, prev
    )
    assert latest_values(node, lake, UNIVERSE, day) != latest_values(node, lake, UNIVERSE, prev)
