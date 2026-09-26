"""Pins TrendlineBreakoutStrategy outputs and the equivalence of the
latest-bar-only trendline signal with the full rolling computation, so the
performance work on the strategy can't change a single number.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stonks.features import library
from stonks.features.library import trendline_breakout_latest, trendline_breakout_signal
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.trendline_breakout import TrendlineBreakoutStrategy

# (bar index, support, resistance, signal) captured from the pre-refactor
# implementation on the fixture series below with lookback=25.
PINNED = [
    (100, 116.13938042609992, 123.67363670322169, -1.0),
    (103, 116.33310013992674, 124.39299826150544, -1.0),
    (106, 114.26674888596075, 125.11233695108061, -1.0),
    (109, 113.83309208055529, 122.27480128311242, -1.0),
    (112, 111.6225521011236, 119.99297884837117, -1.0),
    (115, 106.78036477207033, 115.56715210947078, -1.0),
    (118, 105.46705873850863, 114.15159604049065, -1.0),
    (121, 103.45729969625397, 109.06750351768311, 1.0),
    (124, 99.13833448094161, 107.91058341554039, 1.0),
    (127, 100.53384675203999, 105.92792352019954, 1.0),
    (130, 99.96788019604449, 103.94375344476242, 1.0),
    (133, 98.6118705053798, 103.96615558815292, 1.0),
    (136, 96.19898544086945, 102.49064312346421, -1.0),
    (139, 90.44451020197982, 101.00924844417686, -1.0),
    (142, 87.60007489122988, 98.33059663546665, 1.0),
    (145, 87.54668259016493, 100.94701763058683, 1.0),
    (148, 85.9007535490619, 100.58197527087383, 1.0),
    (151, 90.92954474528828, 99.84169712691435, 1.0),
    (154, 97.1532542362551, 99.75075103087117, 1.0),
    (157, 99.79565661605318, 105.11628441084531, 1.0),
]


@pytest.fixture
def lake_random_walk(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    rng = np.random.default_rng(2024)
    dates = pd.bdate_range("2025-01-02", periods=160)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, len(dates))))
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": "X.US",
                "date": [d.date() for d in dates],
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "adj_close": close,
                "volume": 1,
            }
        )
    )
    yield lake, dates
    lake.close()


def test_strategy_outputs_match_pinned_values(lake_random_walk):
    lake, dates = lake_random_walk
    s = TrendlineBreakoutStrategy({"lookback": 25, "ticker": "X.US"})
    for i, support, resistance, signal in PINNED:
        values = s.extract_features("X.US", dates[i].date(), lake).values
        # Tiny tolerance: the last float bits differ across OSes and BLAS builds.
        assert values["support"] == pytest.approx(support, rel=1e-9, abs=1e-12), i
        assert values["resistance"] == pytest.approx(resistance, rel=1e-9, abs=1e-12), i
        assert values["signal"] == signal, i


def test_strategy_fits_each_window_once_across_consecutive_bars(lake_random_walk, monkeypatch):
    lake, dates = lake_random_walk
    calls = 0
    real = library.fit_trendlines_single

    def counting(window):
        nonlocal calls
        calls += 1
        return real(window)

    monkeypatch.setattr(library, "fit_trendlines_single", counting)
    s = TrendlineBreakoutStrategy({"lookback": 25, "ticker": "X.US"})
    for i in range(60, 160):
        s.estimate_return("X.US", dates[i].date(), lake)
        s.extract_features("X.US", dates[i].date(), lake)
    # 100 bars x 2 calls each; with memoization every distinct window is
    # fitted at most once (<= 160 windows exist in the whole series).
    assert calls <= 160


def _series_zoo() -> list[np.ndarray]:
    rng = np.random.default_rng(99)
    zoo = [100 * np.exp(np.cumsum(rng.normal(0, 0.02, 120))) for _ in range(4)]
    zoo.append(np.full(80, 50.0))  # flat: optimizer edge cases
    zoo.append(np.linspace(10.0, 20.0, 80))  # perfectly linear
    zoo.append(np.concatenate([np.full(40, 100.0), np.linspace(100, 130, 40)]))
    with_nan = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 90)))
    with_nan[30] = np.nan
    zoo.append(with_nan)
    return zoo


@pytest.mark.parametrize("lookback", [5, 12, 20])
def test_latest_matches_full_rolling_signal_on_every_prefix(lookback):
    for series in _series_zoo():
        s_full, r_full, sig_full = trendline_breakout_signal(series, lookback)
        for end in range(1, len(series) + 1):
            s, r, sig = trendline_breakout_latest(series[:end], lookback)
            np.testing.assert_equal(s, s_full[end - 1] if end - 1 >= lookback else np.nan)
            np.testing.assert_equal(r, r_full[end - 1] if end - 1 >= lookback else np.nan)
            assert sig == sig_full[end - 1]


def test_latest_with_shared_cache_matches_without():
    cache: dict = {}
    for series in _series_zoo():
        for end in range(21, len(series) + 1):
            plain = trendline_breakout_latest(series[:end], 20)
            cached = trendline_breakout_latest(series[:end], 20, cache=cache)
            np.testing.assert_equal(cached, plain)
