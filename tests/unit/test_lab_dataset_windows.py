"""LabDataset windows: ratio split by default, explicit train_end override."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.lab.dataset import LabDataset


def test_explicit_train_end_overrides_the_ratio_split():
    ds = LabDataset(
        lake=None,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        train_ratio=0.7,
        train_end=date(2024, 3, 31),
    )
    assert ds.train_window == (date(2024, 1, 1), date(2024, 3, 31))
    assert ds.val_window == (date(2024, 4, 1), date(2024, 12, 31))


def test_ratio_split_when_train_end_is_unset():
    ds = LabDataset(lake=None, start=date(2024, 1, 1), end=date(2024, 1, 11), train_ratio=0.5)
    assert ds.train_window == (date(2024, 1, 1), date(2024, 1, 6))
    assert ds.val_window == (date(2024, 1, 7), date(2024, 1, 11))


@pytest.mark.parametrize("train_end", [date(2023, 12, 31), date(2024, 12, 31)])
def test_train_end_must_leave_a_nonempty_train_and_val_window(train_end):
    with pytest.raises(ValueError):
        LabDataset(lake=None, start=date(2024, 1, 1), end=date(2024, 12, 31), train_end=train_end)
