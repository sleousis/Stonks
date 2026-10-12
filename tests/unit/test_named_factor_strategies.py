"""Named factor strategies: plain-titled entries that hold one library factor
through FactorStrategy, so a trader picks them by name (roadmap 22.2)."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from stonks.lab.catalog import strategy_catalog
from stonks.store.lake import DuckDBLake
from stonks.strategies.base import strategy_metadata
from stonks.strategies.examples.factor_strategy import FactorStrategy
from stonks.strategies.examples.low_vol_defensive import LowVolDefensive
from stonks.strategies.examples.seasonality import Seasonality

DATES = pd.bdate_range("2022-01-03", "2024-03-15")
UNIVERSE = [f"N{i:02d}.US" for i in range(10)]
AS_OF = datetime(2024, 2, 29)

NAMED = [
    (LowVolDefensive, "low_vol_defensive", "low_vol_60"),
    (Seasonality, "seasonality", "seasonality_12m"),
]


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    rng = np.random.default_rng(5)
    frames = []
    for i, ticker in enumerate(UNIVERSE):
        rets = rng.normal(0.0002 * i, 0.03 - 0.0025 * i, len(DATES))
        closes = 100.0 * np.exp(np.cumsum(rets))
        frames.append(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "date": DATES.date,
                    "open": closes,
                    "high": closes * 1.01,
                    "low": closes * 0.99,
                    "close": closes,
                    "adj_close": closes,
                    "volume": 1_000_000.0,
                }
            )
        )
    db = DuckDBLake(tmp_path_factory.mktemp("named") / "lake.duckdb")
    db.migrate()
    db.upsert_prices(pd.concat(frames, ignore_index=True))
    yield db
    db.close()


@pytest.mark.parametrize(("cls", "sid", "factor"), NAMED)
def test_catalogued_by_name_with_a_plain_title(cls, sid, factor):
    assert strategy_catalog()[sid] is cls
    assert issubclass(cls, FactorStrategy)
    assert cls.fixed_factor == factor
    assert "_" not in cls.title
    strategy = cls({})
    assert strategy.factor.id == factor
    assert strategy_metadata(strategy).applicable_asset_classes == tuple(
        strategy.factor.asset_classes
    )
    assert strategy.required_history_bars == strategy.factor.lookback_bars + 1
    assert cls.required_history_bars == strategy.required_history_bars


@pytest.mark.parametrize(("cls", "sid", "factor"), NAMED)
def test_the_factor_is_fixed(cls, sid, factor):
    assert "factor" not in {s.name for s in cls.parameter_spec()}
    with pytest.raises(ValueError, match="factor"):
        cls({"factor": "mom_12_1"})


@pytest.mark.parametrize(("cls", "sid", "factor"), NAMED)
def test_picks_match_the_factor_strategy(cls, sid, factor, lake):
    universe = ",".join(UNIVERSE)
    named = cls({"universe": universe, "top_pct": 0.3})
    generic = FactorStrategy({"factor": factor, "universe": universe, "top_pct": 0.3})
    picks = {t: named.estimate_return(t, AS_OF, lake) for t in UNIVERSE}
    assert picks == {t: generic.estimate_return(t, AS_OF, lake) for t in UNIVERSE}
    assert sum(v is not None for v in picks.values()) == 3


def test_low_vol_holds_the_calmest_names(lake):
    strategy = LowVolDefensive({"universe": ",".join(UNIVERSE), "top_pct": 0.2})
    held = {t for t in UNIVERSE if strategy.estimate_return(t, AS_OF, lake) is not None}
    assert held == {"N08.US", "N09.US"}


@pytest.mark.parametrize(("cls", "sid", "factor"), NAMED)
def test_saved_params_load_back(cls, sid, factor, tmp_path):
    strategy = cls({"top_pct": 0.1})
    strategy.save(tmp_path)
    loaded = cls.load(tmp_path)
    assert loaded.params == strategy.params
    assert loaded.factor.id == factor
