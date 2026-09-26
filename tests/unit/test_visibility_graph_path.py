"""Unit tests for VisibilityGraphPathStrategy."""

from __future__ import annotations

import numpy as np
import pytest

from stonks.features.visibility_graph import (
    average_shortest_path_length,
    natural_visibility_graph,
)
from stonks.strategies.examples.visibility_graph_path import VisibilityGraphPathStrategy
from tests.nt888_bars import as_of, bars, seed_lake

T = "X.CC"


def _lake(tmp_path, tail):
    closes = np.concatenate([np.full(20, 100.0), tail])
    frame = bars(closes)
    return seed_lake(tmp_path / "lake.duckdb", {T: frame}), frame


def test_spec():
    specs = {s.name: s for s in VisibilityGraphPathStrategy.parameter_spec()}
    assert (specs["lookback"].default, specs["lookback"].bounds) == (12, (6, 48))
    assert specs["lookback"].tunable is True


def test_concave_tail_goes_long(tmp_path):
    # concave closes: every interior point is above the chord, so the price
    # graph is a bare path (long paths) while the negated graph is complete.
    lake, frame = _lake(tmp_path, 100 + 10 * np.sqrt(np.arange(1, 13)))
    try:
        s = VisibilityGraphPathStrategy({"ticker": T, "lookback": 12})
        f = s.extract_features(T, as_of(frame, -1), lake).values
        assert f["path_pos"] > f["path_neg"]
        assert f["path_neg"] == pytest.approx(1.0)
        assert f["signal"] == 1.0
        r = s.estimate_return(T, as_of(frame, -1), lake)
        assert r == pytest.approx(f["path_pos"] - f["path_neg"])
    finally:
        lake.close()


def test_convex_tail_stays_flat(tmp_path):
    lake, frame = _lake(tmp_path, 100 + 0.2 * np.arange(1, 13) ** 2)
    try:
        s = VisibilityGraphPathStrategy({"ticker": T, "lookback": 12})
        assert s.estimate_return(T, as_of(frame, -1), lake) is None
        assert s.extract_features(T, as_of(frame, -1), lake).values["signal"] == 0.0
    finally:
        lake.close()


def test_uses_exactly_last_lookback_closes(tmp_path):
    rng = np.random.default_rng(5)
    lake, frame = _lake(tmp_path, 100 + rng.normal(0, 1, 30).cumsum())
    try:
        s = VisibilityGraphPathStrategy({"ticker": T, "lookback": 8})
        window = frame["close"].to_numpy()[-8:]
        f = s.extract_features(T, as_of(frame, -1), lake).values
        expected = average_shortest_path_length(natural_visibility_graph(window))
        assert f["path_pos"] == pytest.approx(expected)
    finally:
        lake.close()
