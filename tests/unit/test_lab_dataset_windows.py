"""RS-17: a LabDataset never has an empty or inverted validation window."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.lab.dataset import LabDataset


def _ds(**kw) -> LabDataset:
    base = {
        "lake": None,
        "universe": ["A.US"],
        "start": date(2024, 1, 1),
        "end": date(2024, 12, 31),
    }
    return LabDataset(**{**base, **kw})  # type: ignore[arg-type]


@pytest.mark.parametrize("ratio", [0.0, 1.0, 1.5, -0.2])
def test_train_ratio_outside_the_open_unit_interval_raises(ratio):
    with pytest.raises(ValueError, match="train_ratio"):
        _ds(train_ratio=ratio)


def test_a_two_day_window_raises():
    with pytest.raises(ValueError, match="validation window"):
        _ds(start=date(2024, 1, 1), end=date(2024, 1, 2))


def test_end_before_start_raises():
    with pytest.raises(ValueError):
        _ds(start=date(2024, 2, 1), end=date(2024, 1, 1))


def test_a_valid_split_keeps_both_windows_non_empty():
    ds = _ds(train_ratio=0.7)
    assert ds.start <= ds.train_window[1] < ds.val_window[0] <= ds.end


def test_explicit_train_end_still_works():
    ds = _ds(train_end=date(2024, 12, 30))
    assert ds.val_window == (date(2024, 12, 31), date(2024, 12, 31))
