"""Cross-sectional ranking toolkit for the learning ranker (roadmap 23.12)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stonks.features.ml import GradientBoostingRegressor
from stonks.features.ranking import (
    cross_sectional_ranks,
    daily_ic,
    ic_summary,
    purged_cv_diagnostic,
    rank_label,
)


def _panel(n_dates=60, n_tickers=12, seed=0):
    """A synthetic factor panel: ``signal`` sets the next-period rank, ``noise``
    carries nothing."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=n_dates)
    tickers = [f"T{i:02d}.US" for i in range(n_tickers)]
    index = pd.MultiIndex.from_product([dates, tickers], names=["timestamp", "ticker"])
    signal = rng.normal(size=len(index))
    noise = rng.normal(size=len(index))
    label = 0.02 * signal + 0.01 * rng.normal(size=len(index))
    return pd.DataFrame({"signal": signal, "noise": noise, "label": label}, index=index)


def test_cross_sectional_ranks_centre_each_date_and_keep_gaps():
    panel = _panel(n_dates=3, n_tickers=4)
    panel.iloc[0, 0] = np.nan
    ranks = cross_sectional_ranks(panel, ["signal", "noise"])
    assert list(ranks.columns) == ["signal", "noise"]
    assert np.isnan(ranks.iloc[0, 0])
    for _, day in ranks.groupby(level="timestamp"):
        vals = day["noise"].to_numpy()
        assert vals.min() > -0.5 and vals.max() <= 0.5
        assert sorted(vals) == sorted(set(vals))  # distinct ranks


def test_cross_sectional_ranks_of_one_name_is_the_top():
    index = pd.MultiIndex.from_tuples(
        [(pd.Timestamp("2024-01-02"), "A")], names=["timestamp", "ticker"]
    )
    ranks = cross_sectional_ranks(pd.DataFrame({"f": [3.0]}, index=index), ["f"])
    assert ranks.iloc[0, 0] == 0.5


def test_rank_label_is_a_percentile_per_date():
    panel = _panel(n_dates=2, n_tickers=5)
    labels = rank_label(panel["label"])
    for _, day in labels.groupby(level="timestamp"):
        assert sorted(day.to_numpy()) == pytest.approx([0.2, 0.4, 0.6, 0.8, 1.0])


def test_daily_ic_is_the_spearman_per_date_and_skips_thin_dates():
    dates = np.array(["2024-01-02"] * 4 + ["2024-01-03"] * 4 + ["2024-01-04"] * 2)
    pred = np.array([1, 2, 3, 4, 4, 3, 2, 1, 1, 2], dtype=float)
    label = np.array([1, 2, 3, 4, 1, 2, 3, 4, 1, 2], dtype=float)
    ic = daily_ic(pred, label, pd.to_datetime(dates))
    assert list(ic.index) == list(pd.to_datetime(["2024-01-02", "2024-01-03"]))
    assert ic.iloc[0] == pytest.approx(1.0) and ic.iloc[1] == pytest.approx(-1.0)


def test_ic_summary_reports_mean_ir_and_hit_rate():
    got = ic_summary(pd.Series([0.1, 0.3, -0.1, 0.1]))
    assert got["ic_mean"] == pytest.approx(0.1)
    assert got["ic_hit_rate"] == pytest.approx(0.75)
    assert got["ic_ir"] == pytest.approx(0.1 / np.std([0.1, 0.3, -0.1, 0.1], ddof=1))
    assert got["n_dates"] == 4
    assert ic_summary(pd.Series([], dtype=float))["n_dates"] == 0


def _xy(panel):
    x = cross_sectional_ranks(panel, ["signal", "noise"])
    y = rank_label(panel["label"])
    dates = panel.index.get_level_values("timestamp")
    t1 = dates + pd.Timedelta(days=7)
    return x.to_numpy(), y.to_numpy(), dates, t1


def test_purged_cv_finds_the_signal_and_ranks_its_importance_first():
    panel = _panel(n_dates=80, n_tickers=15)
    x, y, dates, t1 = _xy(panel)
    diag = purged_cv_diagnostic(
        x,
        y,
        dates,
        t1,
        lambda: GradientBoostingRegressor(max_iter=60, min_samples_leaf=20),
        feature_names=["signal", "noise"],
        folds=3,
        embargo_pct=0.01,
        seed=0,
    )
    assert diag.summary["ic_mean"] > 0.3
    assert diag.importance["signal"] > 0.2
    assert diag.importance["signal"] > 5 * abs(diag.importance["noise"])
    assert np.isfinite(diag.oof).sum() > 0.8 * len(y)


def test_purged_cv_never_trains_on_rows_whose_label_overlaps_a_test_fold():
    panel = _panel(n_dates=40, n_tickers=5)
    x, y, dates, t1 = _xy(panel)
    diag = purged_cv_diagnostic(
        x,
        y,
        dates,
        t1,
        lambda: GradientBoostingRegressor(max_iter=5),
        feature_names=["signal", "noise"],
        folds=4,
        embargo_pct=0.0,
        seed=0,
    )
    t0 = np.asarray(dates)
    t1 = np.asarray(t1)
    assert len(diag.folds) == 4
    for train, test in diag.folds:
        lo, hi = t0[test].min(), t1[test].max()
        assert not ((t0[train] <= hi) & (t1[train] >= lo)).any()


def test_purged_cv_is_deterministic_for_a_seed():
    panel = _panel(n_dates=30, n_tickers=6)
    x, y, dates, t1 = _xy(panel)

    def run():
        return purged_cv_diagnostic(
            x,
            y,
            dates,
            t1,
            lambda: GradientBoostingRegressor(max_iter=10, min_samples_leaf=5),
            feature_names=["signal", "noise"],
            folds=3,
            seed=5,
        )

    a, b = run(), run()
    np.testing.assert_array_equal(a.oof, b.oof)
    assert a.importance == b.importance


def test_purged_cv_needs_two_folds():
    panel = _panel(n_dates=10, n_tickers=3)
    x, y, dates, t1 = _xy(panel)
    with pytest.raises(ValueError):
        purged_cv_diagnostic(
            x, y, dates, t1, GradientBoostingRegressor, feature_names=["a", "b"], folds=1
        )
