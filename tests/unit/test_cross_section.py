"""Cross-sectional helpers (BL-09)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from stonks.features.cross_section import cs_neutralize, cs_rank, cs_winsorize, cs_zscore


def test_cs_rank_is_percentile_and_keeps_nan() -> None:
    x = pd.Series({"A": 3.0, "B": 1.0, "C": np.nan, "D": 2.0})
    out = cs_rank(x)
    assert out["B"] == pytest.approx(1 / 3)
    assert out["D"] == pytest.approx(2 / 3)
    assert out["A"] == pytest.approx(1.0)
    assert math.isnan(out["C"])


def test_cs_rank_averages_ties() -> None:
    out = cs_rank(pd.Series({"A": 1.0, "B": 1.0}))
    assert out.tolist() == [0.75, 0.75]


def test_cs_winsorize_clips_at_mean_plus_minus_k_std() -> None:
    x = pd.Series([0.0] * 9 + [100.0])
    mean, std = x.mean(), x.std(ddof=0)
    out = cs_winsorize(x, n_std=2.0)
    assert out.iloc[-1] == pytest.approx(mean + 2.0 * std)
    assert (out.iloc[:-1] == 0.0).all()


def test_cs_zscore_has_mean_0_std_1_after_winsorising() -> None:
    x = pd.Series(np.r_[np.random.default_rng(3).normal(size=50), 40.0])
    z = cs_zscore(x, winsor=3.0)
    assert z.mean() == pytest.approx(0.0, abs=1e-12)
    assert z.std(ddof=0) == pytest.approx(1.0)
    # no value is left beyond the winsor limit
    assert z.abs().max() <= 3.0 + 1e-6


def test_cs_zscore_hand_value() -> None:
    z = cs_zscore(pd.Series([1.0, 2.0, 3.0]), winsor=None)
    s = math.sqrt(2 / 3)
    assert z.tolist() == pytest.approx([-1 / s, 0.0, 1 / s])


def test_cs_zscore_constant_is_zero() -> None:
    assert cs_zscore(pd.Series([5.0, 5.0, 5.0])).tolist() == [0.0, 0.0, 0.0]


def test_frames_are_processed_row_by_row() -> None:
    df = pd.DataFrame({"A": [1.0, 10.0], "B": [2.0, 30.0], "C": [3.0, 20.0]})
    out = cs_rank(df)
    assert out.loc[1].tolist() == pytest.approx([1 / 3, 1.0, 2 / 3])
    z = cs_zscore(df)
    np.testing.assert_allclose(z.mean(axis=1), 0.0, atol=1e-12)


def test_cs_neutralize_zeroes_each_group_mean() -> None:
    x = pd.Series({"A": 1.0, "B": 3.0, "C": 10.0, "D": 20.0, "E": 7.0})
    groups = pd.Series({"A": "tech", "B": "tech", "C": "energy", "D": "energy", "E": None})
    out = cs_neutralize(x, groups)
    assert out[["A", "B"]].tolist() == [-1.0, 1.0]
    assert out[["C", "D"]].tolist() == [-5.0, 5.0]
    # a ticker with no group forms its own "ungrouped" bucket
    assert out["E"] == 0.0


def test_cs_neutralize_on_frames_uses_one_group_map() -> None:
    df = pd.DataFrame({"A": [1.0, 2.0], "B": [3.0, 6.0], "C": [5.0, 5.0]})
    out = cs_neutralize(df, {"A": "x", "B": "x", "C": "y"})
    assert out.loc[1].tolist() == [-2.0, 2.0, 0.0]


def test_cs_neutralize_without_groups_demeans() -> None:
    out = cs_neutralize(pd.Series([1.0, 2.0, 6.0]), None)
    assert out.tolist() == [-2.0, -1.0, 3.0]
