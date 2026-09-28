"""Lab windows by session (21.3.1, P7, P9): an intraday dataset splits
train and validation by whole trading sessions, with an embargo of whole
sessions between them, and walk-forward folds do the same."""

from __future__ import annotations

import dataclasses
import pickle
from datetime import date, timedelta

import pytest

from stonks.core.interval import Interval
from stonks.lab.dataset import LabDataset, bars_to_sessions, session_dates
from stonks.lab.survival.walk_forward import WalkForwardConfig, session_walk_forward_folds
from stonks.lab.universe_data import prepare_dataset
from stonks.strategies.base import BaseStrategy
from tests.minute_bars import bars_frame, minute_bar, minute_lake, session_frame


def _weekdays(start: date, n: int) -> tuple[date, ...]:
    out, day = [], start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return tuple(out)


SESSIONS = _weekdays(date(2024, 3, 4), 10)  # Mar 4 .. Mar 15
START, END = SESSIONS[0], SESSIONS[-1]


def _ds(**kw) -> LabDataset:
    base = {
        "lake": None,
        "start": START,
        "end": END,
        "train_ratio": 0.7,
        "interval": Interval.MIN_1,
        "sessions": SESSIONS,
    }
    return LabDataset(**{**base, **kw})


# ---- bars to sessions --------------------------------------------------------------


@pytest.mark.parametrize(
    ("bars", "interval", "sessions"),
    [
        (0, Interval.MIN_1, 0),
        (1, Interval.MIN_1, 1),
        (390, Interval.MIN_1, 1),
        (391, Interval.MIN_1, 2),
        (78, Interval.MIN_5, 1),
        (8, Interval.HOUR_1, 2),  # 7 hourly bars fill a 6.5 hour session
        (5, Interval.DAY_1, 5),
    ],
)
def test_bars_to_sessions(bars, interval, sessions):
    assert bars_to_sessions(bars, interval) == sessions


def test_bars_to_sessions_refuses_negative_bars():
    with pytest.raises(ValueError):
        bars_to_sessions(-1, Interval.MIN_1)


# ---- the split ----------------------------------------------------------------------


def test_train_takes_the_first_sessions_and_validation_the_rest():
    ds = _ds()
    assert ds.train_window == (START, SESSIONS[6])  # 7 of 10 sessions
    assert ds.val_window == (SESSIONS[7], END)
    assert ds.window_sessions == SESSIONS


def test_validation_starts_on_the_next_session_across_a_weekend():
    ds = _ds(train_end=date(2024, 3, 8))  # a Friday
    assert ds.val_window[0] == date(2024, 3, 11)  # Monday, not Saturday


def test_embargo_skips_whole_sessions():
    ds = _ds(embargo_bars=390)  # one session of 1m bars
    assert ds.train_window == (START, SESSIONS[6])
    assert ds.val_window == (SESSIONS[8], END)
    assert _ds(embargo_bars=391).val_window == (SESSIONS[9], END)


def test_label_horizon_raises_the_embargo_in_sessions():
    class _Holds(BaseStrategy):
        id = "holds"
        label_horizon_bars = 500

    ds = _ds().for_strategy(_Holds({}))
    assert ds.embargo_bars == 500
    assert ds.val_window[0] == SESSIONS[9]  # two sessions skipped


def test_sessions_outside_the_window_are_ignored():
    ds = _ds(sessions=(date(2024, 3, 1), *SESSIONS, date(2024, 3, 18)))
    assert ds.window_sessions == SESSIONS
    assert ds.train_window == (START, SESSIONS[6])


def test_too_few_sessions_are_refused():
    with pytest.raises(ValueError, match="session"):
        _ds(sessions=SESSIONS[:1])


def test_an_embargo_that_leaves_no_validation_session_is_refused():
    with pytest.raises(ValueError):
        _ds(embargo_bars=390 * 3)


def test_the_train_split_leaves_at_least_one_validation_session():
    ds = _ds(sessions=SESSIONS[:2], end=SESSIONS[1], train_ratio=0.9)
    assert ds.train_window == (START, SESSIONS[0])
    assert ds.val_window == (SESSIONS[1], SESSIONS[1])


def test_without_sessions_the_calendar_split_is_unchanged():
    plain = LabDataset(lake=None, start=START, end=END, interval=Interval.MIN_1)
    assert plain.window_sessions == ()
    assert plain.train_window == (START, START + timedelta(days=int(11 * 0.7)))


def test_the_manifest_records_the_session_count():
    from stonks.lab.manifest import dataset_summary

    assert dataset_summary(_ds())["sessions"] == 10
    plain = LabDataset(lake=None, start=START, end=END, interval=Interval.MIN_1)
    assert "sessions" not in dataset_summary(plain)


def test_detaching_the_lake_keeps_the_sessions_and_pickles():
    ds = dataclasses.replace(_ds(), lake=None)
    assert ds.sessions == SESSIONS
    assert pickle.loads(pickle.dumps(ds)).val_window == ds.val_window


# ---- sessions from the lake ---------------------------------------------------------


def _lake():
    days = [date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 7)]  # no bars on the 6th
    frames = [session_frame("A.US", d, lambda i: 100.0) for d in days]
    pre = minute_bar("A.US", days[0], 0) - timedelta(minutes=30)
    frames.append(bars_frame("A.US", [pre], [100.0]))  # pre-market, same day
    frames.append(session_frame("B.US", date(2024, 3, 6), lambda i: 50.0))
    return minute_lake(frames)


def test_session_dates_are_the_days_the_universe_has_bars():
    lake = _lake()
    try:
        got = session_dates(lake, ["A.US"], Interval.MIN_1, date(2024, 3, 1), date(2024, 3, 8))
        assert got == (date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 7))
        both = session_dates(
            lake, ["A.US", "B.US"], Interval.MIN_1, date(2024, 3, 5), date(2024, 3, 6)
        )
        assert both == (date(2024, 3, 5), date(2024, 3, 6))
        assert (
            session_dates(lake, ["A.US"], Interval.DAY_1, date(2024, 3, 1), date(2024, 3, 8)) == ()
        )
    finally:
        lake.close()


def test_with_sessions_reads_the_lake_once():
    lake = _lake()
    try:
        ds = LabDataset(
            lake=lake,
            universe=["A.US"],
            start=date(2024, 3, 4),
            end=date(2024, 3, 7),
            train_ratio=0.5,
            interval=Interval.MIN_1,
        ).with_sessions()
        assert ds.sessions == (date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 7))
        assert ds.train_window == (date(2024, 3, 4), date(2024, 3, 4))
        assert ds.val_window == (date(2024, 3, 5), date(2024, 3, 7))
    finally:
        lake.close()


def test_with_sessions_on_an_empty_lake_keeps_the_calendar_split():
    lake = minute_lake([session_frame("B.US", date(2024, 3, 6), lambda i: 50.0)])
    try:
        ds = LabDataset(
            lake=lake,
            universe=["A.US"],
            start=date(2024, 3, 4),
            end=date(2024, 3, 8),
            interval=Interval.MIN_1,
        )
        assert ds.with_sessions() is ds
    finally:
        lake.close()


def test_prepare_dataset_splits_intraday_datasets_by_session():
    lake = _lake()
    try:
        common = {
            "lake": lake,
            "universe": ["A.US"],
            "start": date(2024, 3, 4),
            "end": date(2024, 3, 7),
        }
        intraday, _ = prepare_dataset(LabDataset(**common, interval=Interval.MIN_1))
        assert intraday.sessions == (date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 7))
        daily, _ = prepare_dataset(LabDataset(**common))
        assert daily.sessions == ()
    finally:
        lake.close()


# ---- walk-forward by session ------------------------------------------------------------


def test_session_folds_tile_the_last_sessions():
    sessions = _weekdays(date(2024, 3, 4), 20)
    folds = session_walk_forward_folds(sessions, n_splits=2, test_sessions=3, embargo_sessions=1)
    assert [(f.test_start, f.test_end) for f in folds] == [
        (sessions[14], sessions[16]),
        (sessions[17], sessions[19]),
    ]
    # one whole session between each train window and its test window
    assert [f.train_end for f in folds] == [sessions[12], sessions[15]]
    # rolling: the same train length, the first fold from the first session
    assert folds[0].train_start == sessions[0]
    assert folds[1].train_start == sessions[3]


def test_anchored_session_folds_train_from_the_first_session():
    sessions = _weekdays(date(2024, 3, 4), 20)
    folds = session_walk_forward_folds(sessions, n_splits=3, test_sessions=2, anchored=True)
    assert {f.train_start for f in folds} == {sessions[0]}


def test_session_folds_that_do_not_fit_raise():
    with pytest.raises(ValueError):
        session_walk_forward_folds(SESSIONS, n_splits=5, test_sessions=2)
    with pytest.raises(ValueError):
        session_walk_forward_folds(SESSIONS, n_splits=2, test_sessions=3, train_sessions=6)


def test_config_lays_session_folds_over_a_session_dataset():
    sessions = _weekdays(date(2024, 3, 4), 20)
    ds = _ds(sessions=sessions, end=sessions[-1], train_ratio=0.5, embargo_bars=390)
    folds = WalkForwardConfig(n_splits=2).folds_for(ds)
    # the validation window (sessions 11..19, after one embargo session) over two folds
    assert all(f.test_start in sessions and f.test_end in sessions for f in folds)
    assert folds[-1].test_end == sessions[-1]
    for fold in folds:
        fold_ds = dataclasses.replace(
            ds, start=fold.train_start, end=fold.test_end, train_end=fold.train_end
        )
        assert fold_ds.val_window == (fold.test_start, fold.test_end)
        gap = [s for s in sessions if fold.train_end < s < fold.test_start]
        assert len(gap) == 1


def test_matrix_cells_count_sessions_on_a_session_dataset():
    sessions = _weekdays(date(2024, 1, 1), 60)
    ds = _ds(sessions=sessions, start=sessions[0], end=sessions[-1], train_ratio=0.5)
    cfg = WalkForwardConfig(
        n_splits=2, matrix=True, matrix_train_bars=(390 * 10,), matrix_test_bars=(390 * 5,)
    )
    [(train_bars, test_bars, folds)] = cfg.matrix_cells(ds)
    assert (train_bars, test_bars) == (3900, 1950)
    for fold in folds:
        test = [s for s in sessions if fold.test_start <= s <= fold.test_end]
        train = [s for s in sessions if fold.train_start <= s <= fold.train_end]
        assert (len(train), len(test)) == (10, 5)
