"""LabDataset training segments for CV folds (BL-45)."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.lab.dataset import LabDataset


def _ds(**kw) -> LabDataset:
    return LabDataset(lake=None, start=date(2024, 1, 1), end=date(2024, 12, 31), **kw)  # type: ignore[arg-type]


def test_train_windows_default_to_the_train_window():
    ds = _ds()
    assert ds.train_windows == (ds.train_window,)


def test_segments_are_the_train_windows_and_the_longest_is_the_train_window():
    segments = ((date(2024, 1, 1), date(2024, 2, 29)), (date(2024, 6, 1), date(2024, 12, 31)))
    ds = _ds().with_train_segments(segments)
    assert ds.train_windows == segments
    assert ds.train_window == segments[1]


def test_segments_need_no_validation_window():
    # the longest segment ends on the dataset end: fine for a CV fold
    ds = _ds(train_segments=((date(2024, 3, 1), date(2024, 12, 31)),))
    assert ds.train_window == (date(2024, 3, 1), date(2024, 12, 31))


@pytest.mark.parametrize(
    "segments",
    [
        ((date(2023, 12, 1), date(2024, 1, 31)),),
        ((date(2024, 3, 1), date(2024, 2, 1)),),
        ((date(2024, 1, 1), date(2024, 3, 1)), (date(2024, 3, 1), date(2024, 4, 1))),
    ],
)
def test_bad_segments_are_rejected(segments):
    with pytest.raises(ValueError):
        _ds(train_segments=segments)
