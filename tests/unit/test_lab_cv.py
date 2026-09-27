"""Purged k-fold, combinatorial purged k-fold and the CV objective (BL-45)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from math import comb

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import Order
from stonks.lab.cv import (
    CombinatorialPurgedKFold,
    CVObjective,
    PurgedKFold,
    contiguous_runs,
    purged_train_mask,
)
from stonks.lab.dataset import LabDataset
from stonks.lab.objectives import SharpeObjective
from stonks.strategies.base import BaseStrategy

# ---- purging ---------------------------------------------------------------


def test_purge_removes_train_samples_whose_labels_overlap_the_test_block():
    t0 = np.arange(10)
    t1 = t0 + 2  # each label spans three bars
    test = np.zeros(10, dtype=bool)
    test[4:6] = True
    train = purged_train_mask(t0, t1, test, embargo=0)
    # samples 2 and 3 end inside the test block (t1 = 4, 5), and samples 6
    # and 7 start inside the test labels' span (up to bar 7): all purged
    assert list(np.flatnonzero(train)) == [0, 1, 8, 9]


def test_embargo_drops_samples_right_after_the_test_block():
    t0 = np.arange(10)
    t1 = t0.copy()
    test = np.zeros(10, dtype=bool)
    test[4:6] = True
    train = purged_train_mask(t0, t1, test, embargo=2)
    assert list(np.flatnonzero(train)) == [0, 1, 2, 3, 8, 9]


def test_purge_handles_several_test_blocks():
    t0 = np.arange(12)
    t1 = t0 + 1
    test = np.zeros(12, dtype=bool)
    test[[2, 3, 8, 9]] = True
    train = purged_train_mask(t0, t1, test, embargo=1)
    # before each block one sample is purged, after each block one is embargoed
    assert list(np.flatnonzero(train)) == [0, 5, 6, 11]


def test_contiguous_runs():
    assert contiguous_runs(np.array([0, 1, 2, 5, 6, 9])) == [(0, 2), (5, 6), (9, 9)]
    assert contiguous_runs(np.array([], dtype=int)) == []


# ---- purged k-fold -----------------------------------------------------------


def test_purged_kfold_test_folds_cover_every_sample_once():
    cv = PurgedKFold(n_splits=4, embargo_pct=0.0)
    t0 = np.arange(20)
    splits = cv.split(t0, t0 + 3)
    assert len(splits) == 4
    tests = np.concatenate([test for _, test in splits])
    assert sorted(tests.tolist()) == list(range(20))
    for train, test in splits:
        assert not set(train) & set(test)
        # no train label reaches into the test block
        lo, hi = test.min(), test.max()
        assert all(t + 3 < lo or t > hi for t in train)


def test_purged_kfold_embargo_is_a_share_of_the_samples():
    cv = PurgedKFold(n_splits=2, embargo_pct=0.1)
    t0 = np.arange(20)
    (train0, test0), _ = cv.split(t0)
    assert test0.tolist() == list(range(10))
    assert train0.tolist() == list(range(12, 20))  # 2 samples embargoed


def test_purged_kfold_rejects_bad_arguments():
    with pytest.raises(ValueError):
        PurgedKFold(n_splits=1)
    with pytest.raises(ValueError):
        PurgedKFold(embargo_pct=0.6)
    with pytest.raises(ValueError):
        PurgedKFold(n_splits=5).split(np.arange(3))
    with pytest.raises(ValueError):
        PurgedKFold(n_splits=2).split(np.arange(4), np.arange(3))
    with pytest.raises(ValueError):
        PurgedKFold(n_splits=2).split(np.arange(4), np.arange(4) - 1)


def test_purged_kfold_accepts_datetimes():
    t0 = pd.date_range("2024-01-01", periods=10, freq="D").to_numpy()
    t1 = t0 + np.timedelta64(1, "D")
    splits = PurgedKFold(n_splits=2, embargo_pct=0.0).split(t0, t1)
    train, test = splits[0]
    assert test.tolist() == [0, 1, 2, 3, 4]
    assert train.tolist() == [6, 7, 8, 9]  # sample 4's label reaches day 5
    train, test = splits[1]
    assert train.tolist() == [0, 1, 2, 3]  # sample 4's label ends on day 5 too


# ---- combinatorial purged k-fold -------------------------------------------------


def test_cpcv_split_and_path_counts():
    cv = CombinatorialPurgedKFold(n_groups=6, n_test_groups=2)
    assert cv.n_splits == comb(6, 2) == 15
    assert cv.n_paths == 5
    splits = cv.split(np.arange(60))
    assert len(splits) == 15
    assert {s.test_groups for s in splits} == {(a, b) for a in range(6) for b in range(a + 1, 6)}


def test_cpcv_paths_use_each_split_group_exactly_once():
    cv = CombinatorialPurgedKFold(n_groups=6, n_test_groups=2)
    splits = cv.split(np.arange(60))
    paths = cv.paths()
    assert len(paths) == 5
    used: list[tuple[int, int]] = []
    for path in paths:
        assert len(path) == 6
        for group, split_index in enumerate(path):
            assert group in splits[split_index].test_groups
            used.append((split_index, group))
    # every (split, test group) pair backs exactly one path
    assert len(used) == len(set(used)) == 15 * 2


def test_cpcv_other_shapes():
    cv = CombinatorialPurgedKFold(n_groups=4, n_test_groups=1)
    assert cv.n_splits == 4 and cv.n_paths == 1
    cv = CombinatorialPurgedKFold(n_groups=5, n_test_groups=3)
    assert cv.n_splits == 10 and cv.n_paths == 6
    assert len(cv.paths()) == 6


def test_cpcv_train_sets_are_purged_around_every_test_group():
    cv = CombinatorialPurgedKFold(n_groups=4, n_test_groups=2, embargo_pct=0.0)
    t0 = np.arange(40)
    for split in cv.split(t0, t0 + 2):
        train = set(split.train.tolist())
        assert not train & set(split.test.tolist())
        for g in split.test_groups:
            lo, hi = cv.group_bounds(40)[g]
            assert not {lo - 1, lo - 2} & train
        assert split.test_groups == tuple(sorted(split.test_groups))


def test_cpcv_group_bounds_cover_all_samples():
    cv = CombinatorialPurgedKFold(n_groups=3, n_test_groups=1)
    assert cv.group_bounds(10) == [(0, 3), (4, 6), (7, 9)]


def test_cpcv_rejects_bad_arguments():
    with pytest.raises(ValueError):
        CombinatorialPurgedKFold(n_groups=2, n_test_groups=2)
    with pytest.raises(ValueError):
        CombinatorialPurgedKFold(n_groups=6, n_test_groups=0)
    with pytest.raises(ValueError):
        CombinatorialPurgedKFold(n_groups=6).split(np.arange(4))


# ---- CV objective ------------------------------------------------------------------


class _Recorder(BaseStrategy):
    """Buys and holds; records the train windows every fit saw."""

    id = "recorder"
    label_horizon_bars = 3
    fits: list[tuple[tuple[date, date], ...]] = []

    @classmethod
    def parameter_spec(cls):
        return [ParameterSpec(name="x", kind="int", default=1, bounds=(1, 3))]

    def fit(self, dataset):
        type(self).fits.append(tuple(dataset.train_windows))

    def estimate_return(self, ticker, as_of, lake):
        return 1.0

    def decide(self, my_picks, portfolio, prices, as_of):
        return [
            Order(client_id=f"b:{t}:{as_of}", ticker=t, side="buy", quantity=1.0)
            for _, t in my_picks
            if portfolio.positions.get(t, 0.0) <= 0
        ]


def _trend_lake(lake, days: int = 120):
    start = datetime(2024, 1, 1)
    rows = []
    for i in range(days):
        price = 100.0 * (1.001**i) * (1.0 + 0.01 * np.sin(i))
        rows.append(
            {
                "ticker": "AAA.US",
                "timestamp": start + timedelta(days=i),
                "open": price,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "adj_close": price,
                "volume": 1000,
            }
        )
    lake.upsert_bars(pd.DataFrame(rows), interval=Interval.DAY_1)
    return lake


def test_cv_objective_scores_purged_folds_of_the_train_window(lake):
    _trend_lake(lake)
    dataset = LabDataset(
        lake=lake,
        universe=["AAA.US"],
        start=date(2024, 1, 1),
        end=date(2024, 4, 29),
        train_ratio=0.75,
        benchmark="none",
    )
    _Recorder.fits = []
    objective = CVObjective(SharpeObjective(), folds=3)
    assert objective.name == "cv_sharpe"
    assert objective.direction == "maximize"
    outcome = objective.evaluate(_Recorder({}), dataset)
    assert np.isfinite(outcome.score)
    assert len(outcome.returns) == len(outcome.index) > 0
    assert objective.score(_Recorder({}), dataset) == pytest.approx(outcome.score)
    # three fits per evaluation, none of them sees its own test block
    assert len(_Recorder.fits) == 6
    train_start, train_end = dataset.train_window
    for windows in _Recorder.fits[:3]:
        assert windows
        for lo, hi in windows:
            assert train_start <= lo <= hi <= train_end
    # the middle fold trains on two segments either side of its test block
    assert len(_Recorder.fits[1]) == 2


def test_cv_objective_rejects_bad_arguments():
    with pytest.raises(ValueError):
        CVObjective(SharpeObjective(), folds=1)


# ---- the purge horizon is in bars, the folds in days (BE-60) ---------------------------


class _Horizon:
    def __init__(self, bars: int) -> None:
        self.label_horizon_bars = bars


def test_an_intraday_purge_horizon_is_turned_into_trading_days():
    from stonks.lab.cv import purge_days

    daily = LabDataset(lake=None, universe=["A"], start=date(2024, 1, 1), end=date(2024, 6, 1))
    hourly = LabDataset(
        lake=None, universe=["A"], start=date(2024, 1, 1), end=date(2024, 6, 1),
        interval=Interval.HOUR_1,
    )  # fmt: skip
    assert purge_days(daily, _Horizon(24)) == 24
    # 24 hourly bars fill four 6.5-hour sessions, not 24 days
    assert purge_days(hourly, _Horizon(24)) == 4
    assert purge_days(hourly, _Horizon(0)) == 0
