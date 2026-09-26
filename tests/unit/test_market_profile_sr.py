"""Unit tests for MarketProfileSRStrategy."""

from __future__ import annotations

import numpy as np
import pytest

from stonks.features.indicators import atr
from stonks.features.market_profile import market_profile_levels
from stonks.strategies.examples import market_profile_sr as mod
from stonks.strategies.examples.market_profile_sr import MarketProfileSRStrategy
from tests.nt888_bars import as_of, bars, seed_lake

T = "X.CC"
L = 100


def _frame():
    """250 bars ranging tightly around 100, a 10-bar dip to ~96, a climb
    back through 100 to 103, then a drop to 97."""
    rng = np.random.default_rng(7)
    base = 100 + rng.normal(0, 0.3, 250)
    dip = 96 + rng.normal(0, 0.1, 10)
    up = np.linspace(97, 103, 6)
    hold = 103 + rng.normal(0, 0.1, 6)
    down = np.linspace(102, 97, 4)
    return bars(np.concatenate([base, dip, up, hold, down]))


@pytest.fixture
def lake(tmp_path):
    frame = _frame()
    lk = seed_lake(tmp_path / "lake.duckdb", {T: frame})
    yield lk, frame
    lk.close()


def test_spec():
    specs = {s.name: s for s in MarketProfileSRStrategy.parameter_spec()}
    assert (specs["lookback"].default, specs["lookback"].bounds) == (365, (100, 500))
    assert (specs["first_w"].default, specs["first_w"].bounds) == (0.01, (0.01, 1.0))
    assert (specs["atr_mult"].default, specs["atr_mult"].bounds) == (3.0, (1.0, 5.0))
    assert (specs["prom_thresh"].default, specs["prom_thresh"].bounds) == (0.25, (0.1, 0.5))


def test_levels_match_the_feature_helper(lake):
    lk, frame = lake
    s = MarketProfileSRStrategy({"ticker": T, "lookback": L})
    i = 255
    got = s.support_resistance_levels(T, as_of(frame, i), lk)
    win = frame.iloc[i - 2 * L + 1 : i + 1]
    log_atr = atr(np.log(win["high"]), np.log(win["low"]), np.log(win["close"]), L).iloc[-1]
    expected = market_profile_levels(np.log(win["close"].to_numpy()[-L:]), log_atr, 0.01, 3.0, 0.25)
    assert got == pytest.approx(expected)
    assert any(abs(level - 100) < 1 for level in got)


def test_long_after_crossing_up_flat_after_crossing_down(lake):
    lk, frame = lake
    s = MarketProfileSRStrategy({"ticker": T, "lookback": L})
    assert s.estimate_return(T, as_of(frame, 259), lk) is None  # in the dip
    # the climb crosses the ~100 level somewhere in bars 260..265
    longs = [s.estimate_return(T, as_of(frame, i), lk) for i in range(260, 272)]
    assert any(r is not None and r > 0 for r in longs)
    assert s.extract_features(T, as_of(frame, 271), lk).values["signal"] == 1.0
    # the final drop crosses back below
    assert s.estimate_return(T, as_of(frame, -1), lk) is None


def test_level_computation_is_cached_per_bar(lake, monkeypatch):
    lk, frame = lake
    calls = []
    real = mod.market_profile_levels

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(mod, "market_profile_levels", counting)
    s = MarketProfileSRStrategy({"ticker": T, "lookback": L})
    s.estimate_return(T, as_of(frame, 270), lk)
    first = len(calls)
    assert first > 1
    s.estimate_return(T, as_of(frame, 270), lk)
    assert len(calls) == first  # all cached
    s.estimate_return(T, as_of(frame, 271), lk)
    assert len(calls) == first + 1  # only the new bar


def test_needs_two_lookbacks_of_history(lake):
    lk, frame = lake
    s = MarketProfileSRStrategy({"ticker": T, "lookback": L})
    assert s.extract_features(T, as_of(frame, 2 * L - 2), lk).values == {}
    assert s.extract_features(T, as_of(frame, 2 * L - 1), lk).values != {}
