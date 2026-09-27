"""Edge cases every catalogued strategy must handle (review 18.1 list):
finite features on flat prices, NaN closes inside the window, and
cross-sectional strategies on one ticker and with a delisted ticker."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stonks.lab.catalog import strategy_catalog
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.quant_momentum import QuantMomentum
from stonks.strategies.examples.stocks_on_the_move import StocksOnTheMove
from stonks.strategies.examples.volatility_hawkes import VolatilityHawkesStrategy
from stonks.strategies.examples.vsa import VSAStrategy
from tests.nt888_bars import as_of, bars, seed_lake

N_DAYS = 420
DATES = pd.bdate_range("2023-01-02", periods=N_DAYS)
LAST = DATES[-1].date()


def _lake(closes_by_ticker: dict[str, np.ndarray], dates=DATES) -> DuckDBLake:
    lake = DuckDBLake(Path(":memory:"))
    lake.migrate()
    frames = []
    for ticker, closes in closes_by_ticker.items():
        d = dates[: len(closes)]
        frames.append(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "date": [x.date() for x in d],
                    "open": closes,
                    "high": closes,
                    "low": closes,
                    "close": closes,
                    "adj_close": closes,
                    "volume": 1_000_000.0,
                }
            )
        )
        lake.con.execute("INSERT INTO instruments (id, asset_class) VALUES (?, 'equity')", [ticker])
    lake.upsert_prices(pd.concat(frames, ignore_index=True))
    return lake


def _build(cls):
    names = {s.name for s in cls.parameter_spec()}
    params = {}
    if "ticker" in names:
        params["ticker"] = "A.US"
    if "interval" in names:
        # an intraday-only strategy keeps its own minute interval
        spec = next(s for s in cls.parameter_spec() if s.name == "interval")
        daily = spec.bounds is None or "1d" in spec.bounds
        params["interval"] = "1d" if daily else spec.default
    if "universe" in names:
        params["universe"] = "A.US,B.US"
    return cls(params)


def _finite(values: dict) -> bool:
    return all(isinstance(v, int | float) and math.isfinite(v) for v in values.values())


@pytest.fixture(scope="module")
def flat_lake():
    lake = _lake({"A.US": np.full(N_DAYS, 50.0), "B.US": np.full(N_DAYS, 20.0)})
    yield lake
    lake.close()


@pytest.fixture(scope="module")
def nan_lake():
    rng = np.random.default_rng(1)
    closes = {
        t: 50.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, N_DAYS))) for t in ("A.US", "B.US")
    }
    for c in closes.values():
        c[-30:-25] = np.nan  # a hole of NaN closes inside every lookback
    lake = _lake(closes)
    yield lake
    lake.close()


@pytest.mark.parametrize("name", sorted(strategy_catalog()))
def test_features_are_finite_on_flat_prices(name, flat_lake):
    strategy = _build(strategy_catalog()[name])
    for ticker in ("A.US", "B.US"):
        values = strategy.extract_features(ticker, LAST, flat_lake).values
        assert _finite(values), {k: v for k, v in values.items() if not _finite({k: v})}
        r = strategy.estimate_return(ticker, LAST, flat_lake)
        assert r is None or math.isfinite(r)


@pytest.mark.parametrize("name", sorted(strategy_catalog()))
def test_nan_closes_inside_the_window_never_crash_or_leak_nan(name, nan_lake):
    strategy = _build(strategy_catalog()[name])
    for ticker in ("A.US", "B.US"):
        r = strategy.estimate_return(ticker, LAST, nan_lake)
        assert r is None or math.isfinite(r)


def _untriggered_bars(n: int = 700):
    """Hourly bars where neither trigger fires: the range tracks volume
    exactly (no VSA anomaly) and widens geometrically, so the Hawkes vol never
    dips below its rolling 5% quantile."""
    rng = np.random.default_rng(5)
    closes = 100 + rng.normal(0, 0.05, n).cumsum()
    spread = np.geomspace(0.5, 50.0, n)
    return bars(closes, spread=spread, volume=spread * 1000)


@pytest.mark.parametrize(
    ("cls", "untriggered"),
    [
        (VSAStrategy, {"trigger_dev": 0.0, "bars_since_trigger": 24.0}),
        (VolatilityHawkesStrategy, {}),
    ],
    ids=["vsa", "volatility_hawkes"],
)
def test_features_are_finite_when_nothing_has_triggered(tmp_path, cls, untriggered):
    """Intraday bars where no trigger fired still give finite features with
    their documented no-trigger values."""
    frame = _untriggered_bars()
    lake = seed_lake(tmp_path / "lake.duckdb", {"X.CC": frame})
    try:
        strategy = cls({"ticker": "X.CC"})
        values = strategy.extract_features("X.CC", as_of(frame, -1), lake).values
        assert values, "enough history for a full evaluation"
        assert values["signal"] == 0.0
        assert _finite(values), {k: v for k, v in values.items() if not _finite({k: v})}
        for key, expected in untriggered.items():
            assert values[key] == expected
        if cls is VolatilityHawkesStrategy:
            assert values["close_at_last_below"] == values["close"]
    finally:
        lake.close()


# ---- cross-sectional strategies --------------------------------------------------

CROSS_SECTIONAL = [QuantMomentum, StocksOnTheMove]


@pytest.mark.parametrize("cls", CROSS_SECTIONAL, ids=lambda c: c.__name__)
def test_cross_section_of_one_ticker(cls):
    rng = np.random.default_rng(2)
    lake = _lake({"A.US": 50.0 * np.exp(np.cumsum(rng.normal(0.001, 0.01, N_DAYS)))})
    try:
        strategy = cls({"universe": "A.US", "index_ticker": ""} if cls is StocksOnTheMove else {})
        r = strategy.estimate_return("A.US", LAST, lake)
        assert r is None or math.isfinite(r)
        # asking again is order independent and stable
        assert strategy.estimate_return("A.US", LAST, lake) == r
    finally:
        lake.close()


@pytest.mark.parametrize("cls", CROSS_SECTIONAL, ids=lambda c: c.__name__)
def test_delisted_ticker_never_ranks(cls):
    rng = np.random.default_rng(3)
    alive = {t: 50.0 * np.exp(np.cumsum(rng.normal(0.001, 0.01, N_DAYS))) for t in ("A.US", "B.US")}
    # D.US rose fastest of all, then stopped trading 40 sessions ago
    dead = 50.0 * np.exp(np.cumsum(np.full(N_DAYS - 40, 0.004)))
    lake = _lake({**alive, "D.US": dead})
    try:
        params = {"universe": "A.US,B.US,D.US"}
        if cls is StocksOnTheMove:
            params["index_ticker"] = ""
        strategy = cls(params)
        assert strategy.estimate_return("D.US", LAST, lake) is None
    finally:
        lake.close()
