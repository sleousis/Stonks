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


# ---- embargo (BL-20) -------------------------------------------------------------


def test_embargo_leaves_a_gap_before_every_test_window_and_keeps_the_test_windows():
    plain = walk_forward_folds(START, END, n_splits=3, test_days=30, train_days=90)
    gapped = walk_forward_folds(
        START, END, n_splits=3, test_days=30, train_days=90, embargo_days=10
    )
    assert [(f.test_start, f.test_end) for f in gapped] == [
        (f.test_start, f.test_end) for f in plain
    ]
    for f in gapped:
        assert f.train_end + timedelta(days=11) == f.test_start  # 10 embargo days in between
        assert (f.train_end - f.train_start).days + 1 == 90


def test_default_train_length_still_starts_the_first_fold_at_the_dataset_start():
    folds = walk_forward_folds(START, END, n_splits=2, test_days=100, embargo_days=7)
    assert folds[0].train_start == START
    assert folds[0].train_end + timedelta(days=8) == folds[0].test_start
    lengths = {(f.train_end - f.train_start).days for f in folds}
    assert len(lengths) == 1


def test_an_embargo_that_eats_the_train_window_raises():
    with pytest.raises(ValueError, match="embargo"):
        walk_forward_folds(START, END, n_splits=1, test_days=300, embargo_days=70)


def test_config_takes_the_embargo_from_the_dataset_and_the_label_horizon():
    from stonks.core.interval import Interval
    from stonks.lab.dataset import embargo_calendar_days

    class _Labelled:
        label_horizon_bars = 10

    ds = LabDataset(lake=None, start=START, end=END, train_ratio=0.7, embargo_bars=2)
    cfg = WalkForwardConfig(n_splits=3, test_days=30)
    gap = embargo_calendar_days(2, Interval.DAY_1)
    assert all(f.train_end + timedelta(days=1 + gap) == f.test_start for f in cfg.folds_for(ds))
    wide = embargo_calendar_days(10, Interval.DAY_1)
    folds = cfg.folds_for(ds, _Labelled())
    assert all(f.train_end + timedelta(days=1 + wide) == f.test_start for f in folds)


def test_matrix_cells_skip_the_cells_that_do_not_fit():
    ds = LabDataset(lake=None, start=START, end=END, train_ratio=0.7)
    cfg = WalkForwardConfig(
        n_splits=2, matrix=True, matrix_train_bars=(60, 1000), matrix_test_bars=(20, 40)
    )
    cells = cfg.matrix_cells(ds)
    assert [(tb, sb) for tb, sb, _ in cells] == [(60, 20), (60, 40)]
    for _, _, folds in cells:
        _check_common(folds, 2)


def test_new_config_defaults():
    cfg = WalkForwardConfig()
    assert cfg.min_wfe == 0.5
    assert cfg.matrix is False
    assert cfg.matrix_train_bars == (504, 756, 1008)
    assert cfg.matrix_test_bars == (126, 252)
    assert cfg.max_workers is None
    with pytest.raises(pydantic.ValidationError):
        WalkForwardConfig(min_wfe=1.5)
