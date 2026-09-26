"""LabDataset embargo (BL-20): a gap of trading bars between train and
validation, at least as long as the strategy's label horizon."""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta

import pytest

from stonks.core.interval import Interval
from stonks.lab.dataset import LabDataset, embargo_calendar_days, scoring_window

START, END = date(2024, 1, 1), date(2024, 12, 31)


def _weekdays_between(a: date, b: date) -> int:
    """Weekdays strictly between ``a`` and ``b``."""
    return sum(1 for i in range(1, (b - a).days) if (a + timedelta(days=i)).weekday() < 5)


def test_zero_embargo_keeps_the_day_after_train_end():
    ds = LabDataset(lake=None, start=START, end=END, train_end=date(2024, 3, 29))
    assert ds.embargo_bars == 0
    assert ds.val_window == (date(2024, 3, 30), END)


def test_embargo_shifts_the_validation_start_but_not_the_train_window():
    plain = LabDataset(lake=None, start=START, end=END, train_end=date(2024, 3, 29))
    gapped = dataclasses.replace(plain, embargo_bars=5)
    assert gapped.train_window == plain.train_window
    assert gapped.val_window[0] > plain.val_window[0]
    assert gapped.val_window[1] == END
    assert gapped.val_window[0] == date(2024, 3, 30) + timedelta(
        days=embargo_calendar_days(5, Interval.DAY_1)
    )


@pytest.mark.parametrize("bars", [1, 2, 3, 5, 10, 21, 63, 126])
@pytest.mark.parametrize("weekday_offset", range(7))
def test_embargo_skips_at_least_that_many_trading_days(bars, weekday_offset):
    train_end = date(2024, 3, 25) + timedelta(days=weekday_offset)  # Mon + offset
    ds = LabDataset(lake=None, start=START, end=END, train_end=train_end, embargo_bars=bars)
    val_start = ds.val_window[0]
    # every weekday in the gap is an exchange session at most; the gap
    # holds at least ``bars`` of them (plus slack for holidays)
    assert _weekdays_between(train_end, val_start) >= bars
    # ... and is not wildly longer than the embargo
    assert (val_start - train_end).days <= bars * 1.5 + 5


def test_calendar_conversion_goes_through_the_bar_interval():
    assert embargo_calendar_days(0, Interval.DAY_1) == 0
    # one session's worth of 5-minute bars is about one trading day
    assert embargo_calendar_days(78, Interval.MIN_5) == embargo_calendar_days(1, Interval.DAY_1)
    # a weekly bar spans a calendar week
    assert embargo_calendar_days(1, Interval.WEEK_1) >= 7


def test_embargo_that_swallows_the_validation_window_is_rejected():
    with pytest.raises(ValueError, match="embargo"):
        LabDataset(
            lake=None, start=START, end=END, train_end=date(2024, 12, 20), embargo_bars=20
        )


def test_negative_embargo_is_rejected():
    with pytest.raises(ValueError):
        LabDataset(lake=None, start=START, end=END, embargo_bars=-1)


class _Labelled:
    def __init__(self, horizon: int) -> None:
        self.label_horizon_bars = horizon


def test_label_horizon_wins_over_a_smaller_embargo():
    ds = LabDataset(lake=None, start=START, end=END, train_end=date(2024, 6, 28), embargo_bars=3)
    assert ds.effective_embargo_bars(_Labelled(10)) == 10
    assert ds.for_strategy(_Labelled(10)).embargo_bars == 10
    assert ds.for_strategy(_Labelled(10)).val_window == dataclasses.replace(
        ds, embargo_bars=10
    ).val_window
    # a larger embargo is kept; no horizon at all is a horizon of 0
    assert ds.effective_embargo_bars(_Labelled(2)) == 3
    assert ds.effective_embargo_bars(object()) == 3
    assert ds.for_strategy(object()) is ds


def test_scoring_window_is_the_embargoed_validation_window_by_default():
    ds = LabDataset(lake=None, start=START, end=END, train_end=date(2024, 6, 28))
    strategy = _Labelled(5)
    assert scoring_window(ds, strategy) == ds.for_strategy(strategy).val_window
    assert scoring_window(ds, strategy, "val") == ds.for_strategy(strategy).val_window
    assert scoring_window(ds, strategy, "full") == ds.full_window
    with pytest.raises(ValueError):
        scoring_window(ds, strategy, "train")


def test_scoring_window_works_on_duck_typed_contexts():
    class _Ctx:
        val_window = (date(2024, 5, 1), END)
        full_window = (START, END)

    assert scoring_window(_Ctx(), _Labelled(5)) == (date(2024, 5, 1), END)


def test_benchmark_defaults_to_auto():
    assert LabDataset(lake=None).benchmark == "auto"


def test_stitched_oos_report_is_unset_and_never_copied():
    ds = LabDataset(lake=None, start=START, end=END)
    assert ds.stitched_oos_report is None
    ds.stitched_oos_report = "report"
    assert dataclasses.replace(ds, embargo_bars=1).stitched_oos_report is None
