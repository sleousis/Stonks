"""Walk-forward fold geometry and config validation."""

from __future__ import annotations

from datetime import date, timedelta

import pydantic
import pytest

from stonks.lab.dataset import LabDataset
from stonks.lab.survival.walk_forward import WalkForwardConfig, walk_forward_folds

START, END = date(2024, 1, 1), date(2024, 12, 31)  # 366 days


def _check_common(folds, n, end=END):
    assert len(folds) == n
    assert [f.index for f in folds] == list(range(n))
    assert folds[-1].test_end == end
    for f in folds:
        assert f.train_start <= f.train_end
        assert f.train_end + timedelta(days=1) == f.test_start  # no gap, no overlap
        assert f.test_start <= f.test_end
    for prev, nxt in zip(folds, folds[1:], strict=False):
        assert prev.test_end + timedelta(days=1) == nxt.test_start  # contiguous OOS


def test_rolling_folds_keep_a_constant_train_length():
    folds = walk_forward_folds(START, END, n_splits=3, test_days=30, train_days=90)
    _check_common(folds, 3)
    assert {(f.train_end - f.train_start).days + 1 for f in folds} == {90}
    assert {(f.test_end - f.test_start).days + 1 for f in folds} == {30}
    assert folds[0].test_start == END - timedelta(days=89)


def test_anchored_folds_all_train_from_the_start():
    folds = walk_forward_folds(START, END, n_splits=4, test_days=30, anchored=True)
    _check_common(folds, 4)
    assert {f.train_start for f in folds} == {START}
    assert folds[1].train_end > folds[0].train_end


def test_rolling_default_train_length_is_everything_before_the_first_test():
    folds = walk_forward_folds(START, END, n_splits=2, test_days=100)
    _check_common(folds, 2)
    assert folds[0].train_start == START
    first_len = (folds[0].train_end - folds[0].train_start).days
    assert {(f.train_end - f.train_start).days for f in folds} == {first_len}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"n_splits": 4, "test_days": 100},  # 400 test days > span
        {"n_splits": 3, "test_days": 30, "train_days": 300},  # first train starts too early
    ],
)
def test_too_short_a_span_raises(kwargs):
    with pytest.raises(ValueError):
        walk_forward_folds(START, END, **kwargs)


def test_config_splits_the_validation_window_over_the_folds_by_default():
    # works the same for a ratio split and an explicit train_end
    ratio = LabDataset(lake=None, start=START, end=END, train_ratio=0.7)
    explicit = LabDataset(lake=None, start=START, end=END, train_end=date(2024, 6, 30))
    cfg = WalkForwardConfig(n_splits=3)
    val_days = (END - ratio.val_window[0]).days + 1
    assert cfg.resolved_test_days(ratio.val_window) == val_days // 3
    assert cfg.resolved_test_days(explicit.val_window) == 184 // 3
    assert WalkForwardConfig(test_days=10).resolved_test_days(ratio.val_window) == 10


def test_config_lays_its_folds_over_a_dataset():
    ds = LabDataset(lake=None, start=START, end=END, train_ratio=0.7)
    cfg = WalkForwardConfig(n_splits=3, train_days=60, anchored=False)
    assert cfg.folds_for(ds) == walk_forward_folds(
        START,
        END,
        n_splits=3,
        test_days=cfg.resolved_test_days(ds.val_window),
        train_days=60,
        anchored=False,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"n_splits": 0},
        {"test_days": 0},
        {"train_days": 0},
        {"min_positive_share": 1.5},
        {"metric": "profit_factor"},
    ],
)
def test_config_rejects_bad_values(kwargs):
    with pytest.raises(pydantic.ValidationError):
        WalkForwardConfig(**kwargs)
