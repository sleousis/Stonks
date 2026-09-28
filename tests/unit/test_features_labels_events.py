"""Fractional differencing, the CUSUM event filter and trend-scanning labels
(roadmap 23.10). Every causal tool gets a future-shock test: changing the
bars after ``t`` must not change anything at or before ``t``."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stonks.features.labels import (
    ADFTest,
    StationarityResult,
    StationarityTest,
    cusum_events,
    ffd_weights,
    frac_diff_ffd,
    min_ffd_d,
    trend_scanning,
)


def _series(values) -> pd.Series:
    return pd.Series(
        np.asarray(values, dtype=float),
        index=pd.date_range("2024-01-01", periods=len(values), freq="D"),
    )


def _random_walk(n: int = 600, seed: int = 0) -> pd.Series:
    rng = np.random.default_rng(seed)
    return _series(np.cumsum(rng.normal(0, 0.01, n)) + 5.0)


def _shock_after(series: pd.Series, t: int, size: float = 10.0) -> pd.Series:
    shocked = series.copy()
    shocked.iloc[t + 1 :] = shocked.iloc[t + 1 :] + size
    return shocked


# ---- fractional differencing --------------------------------------------------


def test_ffd_weights_follow_the_binomial_recursion():
    w = ffd_weights(0.5, threshold=1e-3)
    assert w[0] == 1.0
    assert w[1] == pytest.approx(-0.5)
    assert w[2] == pytest.approx(-0.125)
    assert abs(w[-1]) >= 1e-3
    assert len(ffd_weights(0.5, threshold=1e-5)) > len(w)


def test_ffd_weights_of_whole_d_are_plain_differences():
    np.testing.assert_allclose(ffd_weights(1.0, threshold=1e-8), [1.0, -1.0])
    np.testing.assert_allclose(ffd_weights(0.0, threshold=1e-8), [1.0])


def test_ffd_weights_fixed_width_caps_the_window():
    assert len(ffd_weights(0.4, threshold=1e-9, width=10)) == 10


def test_ffd_weights_reject_bad_input():
    with pytest.raises(ValueError):
        ffd_weights(-0.1)
    with pytest.raises(ValueError):
        ffd_weights(0.5, threshold=0.0)
    with pytest.raises(ValueError):
        ffd_weights(0.5, width=0)


def test_frac_diff_with_d_one_is_the_first_difference():
    x = _random_walk(50)
    out = frac_diff_ffd(x, 1.0)
    pd.testing.assert_series_equal(out.iloc[1:], x.diff().iloc[1:], check_names=False)
    assert np.isnan(out.iloc[0])


def test_frac_diff_warm_up_is_nan_then_finite():
    x = _random_walk(200)
    width = len(ffd_weights(0.4, threshold=1e-3))
    out = frac_diff_ffd(x, 0.4, threshold=1e-3)
    assert out.iloc[: width - 1].isna().all()
    assert out.iloc[width - 1 :].notna().all()


def test_frac_diff_future_shock_leaves_the_past_alone():
    x = _random_walk(300)
    t = 200
    base = frac_diff_ffd(x, 0.35, threshold=1e-3)
    shocked = frac_diff_ffd(_shock_after(x, t), 0.35, threshold=1e-3)
    pd.testing.assert_series_equal(base.iloc[: t + 1], shocked.iloc[: t + 1])
    assert not np.allclose(base.iloc[t + 1 :], shocked.iloc[t + 1 :])


class _ThresholdTest(StationarityTest):
    """Stationary once ``d`` reaches 0.3: reads the lag-1 autocorrelation."""

    def run(self, values: np.ndarray) -> StationarityResult:
        v = np.asarray(values, dtype=float)
        rho = float(np.corrcoef(v[:-1], v[1:])[0, 1])
        return StationarityResult(statistic=rho, pvalue=0.01 if rho < 0.9 else 0.5)


def test_min_ffd_d_picks_the_smallest_passing_d_and_lists_every_try():
    x = _random_walk(800)
    result = min_ffd_d(x, d_grid=[0.0, 0.1, 0.2, 0.3, 0.5, 1.0], test=_ThresholdTest())
    assert result.d is not None
    assert result.d > 0.0
    tried = [d for d, _ in result.tried]
    assert tried == sorted(tried)
    assert tried[-1] == result.d  # stops at the first pass
    assert all(p >= 0.05 for d, p in result.tried[:-1])
    assert result.n_tried == len(result.tried)


def test_min_ffd_d_returns_none_when_nothing_passes():
    class Never(StationarityTest):
        def run(self, values):
            return StationarityResult(statistic=0.0, pvalue=0.9)

    result = min_ffd_d(_random_walk(200), d_grid=[0.2, 0.5], threshold=1e-2, test=Never())
    assert result.d is None
    assert result.n_tried == 2


def test_adf_test_tells_a_random_walk_from_white_noise():
    rng = np.random.default_rng(1)
    noise = rng.normal(size=500)
    walk = np.cumsum(noise)
    adf = ADFTest()
    assert adf.run(noise).pvalue < 0.05
    assert adf.run(walk).pvalue > 0.05


def test_min_ffd_d_with_adf_finds_d_below_one_for_a_random_walk():
    result = min_ffd_d(_random_walk(1500), d_grid=np.linspace(0, 1, 11), threshold=1e-3)
    assert result.d is not None
    assert 0.0 < result.d <= 1.0


# ---- CUSUM filter -------------------------------------------------------------


def test_cusum_fires_after_the_drift_crosses_the_threshold_and_resets():
    x = _series([0.0, 0.3, 0.6, 0.9, 1.2, 1.2, 1.2, 0.9, 0.6, 0.3])
    events = cusum_events(x, threshold=0.5)
    assert list(events) == [x.index[2], x.index[4], x.index[8]]


def test_cusum_accepts_a_threshold_series():
    x = _series([0.0, 0.3, 0.6, 0.9, 1.2])
    h = pd.Series([1.0, 1.0, 1.0, 0.2, 0.2], index=x.index)
    assert list(cusum_events(x, threshold=h)) == [x.index[3], x.index[4]]


def test_cusum_future_shock_keeps_earlier_events():
    x = _random_walk(400)
    t = 250
    base = cusum_events(x, threshold=0.02)
    shocked = cusum_events(_shock_after(x, t), threshold=0.02)
    cut = x.index[t]
    assert list(base[base <= cut]) == list(shocked[shocked <= cut])


def test_cusum_rejects_a_non_positive_threshold():
    with pytest.raises(ValueError):
        cusum_events(_random_walk(20), threshold=0.0)


# ---- trend scanning -----------------------------------------------------------


def test_trend_scanning_labels_the_sign_of_the_strongest_trend():
    up = np.linspace(0, 1, 30)
    down = np.linspace(1, 0, 30)
    rng = np.random.default_rng(0)
    x = _series(np.concatenate([up, down[1:]]) + rng.normal(0, 0.01, 59))
    labels = trend_scanning(x, events=[0, 35], spans=range(5, 15))
    assert list(labels["label"]) == [1, -1]
    assert labels["tval"].iloc[0] > 0 > labels["tval"].iloc[1]
    row = labels.iloc[0]
    assert row["t1_pos"] == row["t0_pos"] + row["span"] - 1
    assert row["t1"] == x.index[int(row["t1_pos"])]
    assert bool(row["complete"])


def test_trend_scanning_marks_events_near_the_end_incomplete():
    x = _random_walk(40)
    labels = trend_scanning(x, events=[5, 32], spans=range(5, 12))
    assert list(labels["complete"]) == [True, False]
    assert labels["t1_pos"].iloc[1] <= 39


def test_trend_scanning_reads_only_its_own_window():
    x = _random_walk(200)
    spans = range(5, 20)
    base = trend_scanning(x, events=[10, 50], spans=spans)
    last_read = 50 + max(spans) - 1
    shocked = trend_scanning(_shock_after(x, last_read), events=[10, 50], spans=spans)
    pd.testing.assert_frame_equal(base, shocked)


def test_trend_scanning_rejects_short_spans():
    with pytest.raises(ValueError):
        trend_scanning(_random_walk(30), spans=[2, 5])
