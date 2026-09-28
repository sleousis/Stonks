"""Daily P&L from portfolio snapshots (roadmap 2.5c)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from stonks.production.pnl import daily_pnl, load_pnl


def test_daily_pnl_math():
    rows = daily_pnl(
        [
            (date(2026, 1, 1), 100.0),
            (date(2026, 1, 2), 110.0),
            (date(2026, 1, 3), 99.0),
            (date(2026, 1, 4), 121.0),
        ]
    )
    assert [r.day for r in rows] == [date(2026, 1, d) for d in (1, 2, 3, 4)]
    assert rows[0].daily_change is None
    assert rows[0].daily_return is None
    assert rows[0].cumulative_return == 0.0
    assert rows[0].drawdown == 0.0

    assert rows[1].daily_change == pytest.approx(10.0)
    assert rows[1].daily_return == pytest.approx(0.10)
    assert rows[1].cumulative_return == pytest.approx(0.10)
    assert rows[1].drawdown == 0.0

    assert rows[2].daily_change == pytest.approx(-11.0)
    assert rows[2].daily_return == pytest.approx(-0.10)
    assert rows[2].cumulative_return == pytest.approx(-0.01)
    assert rows[2].drawdown == pytest.approx(-0.10)  # 99 / 110 - 1

    assert rows[3].cumulative_return == pytest.approx(0.21)
    assert rows[3].drawdown == 0.0


def test_daily_pnl_empty():
    assert daily_pnl([]) == []


def test_daily_pnl_since_keeps_inception_baseline():
    points = [(date(2026, 1, 1), 100.0), (date(2026, 1, 2), 120.0), (date(2026, 1, 3), 90.0)]
    rows = daily_pnl(points, since=date(2026, 1, 3))
    [row] = rows
    assert row.daily_change == pytest.approx(-30.0)
    assert row.cumulative_return == pytest.approx(-0.10)
    assert row.drawdown == pytest.approx(-0.25)


def test_daily_pnl_zero_value_does_not_divide_by_zero():
    rows = daily_pnl([(date(2026, 1, 1), 0.0), (date(2026, 1, 2), 10.0)])
    assert rows[1].daily_return is None
    assert rows[1].cumulative_return is None


def _snap(state, tick_id, taken_at, value):
    state.execute(
        "INSERT OR IGNORE INTO tick_runs (id, started_at, status) VALUES (?, ?, 'ok')",
        [tick_id, taken_at],
    )
    state.execute(
        "INSERT INTO portfolio_snapshots (tick_id, taken_at, cash, positions_json, total_value) "
        "VALUES (?, ?, ?, '{}', ?)",
        [tick_id, taken_at, value, value],
    )


def test_load_pnl_takes_last_snapshot_per_utc_day(state):
    _snap(state, "t1", "2026-01-01T15:00:00+00:00", 100.0)
    _snap(state, "t2", "2026-01-02T15:00:00+00:00", 105.0)
    _snap(state, "t3", "2026-01-02T20:00:00+00:00", 108.0)  # rerun later that day
    rows = load_pnl(state)
    assert [(r.day, r.total_value) for r in rows] == [
        (date(2026, 1, 1), 100.0),
        (date(2026, 1, 2), 108.0),
    ]


def test_load_pnl_for_shadow_strategy(state):
    state.execute(
        "INSERT INTO strategies (id, class_path, params_json, status, created_at, updated_at) "
        "VALUES ('s1', 'x:Y', '{}', 'shadow', '2026-01-01', '2026-01-01')"
    )
    state.execute(
        "INSERT INTO tick_runs (id, started_at, status) VALUES ('t1', '2026-01-01', 'ok')"
    )
    for as_of, value in (("2026-01-01", 1000.0), ("2026-01-02", 1100.0)):
        state.execute(
            "INSERT INTO shadow_portfolio_snapshots "
            "(tick_id, strategy_id, as_of, taken_at, cash, positions_json, total_value) "
            "VALUES ('t1', 's1', ?, ?, 0, ?, ?)",
            [as_of, as_of, json.dumps({}), value],
        )
    rows = load_pnl(state, strategy_id="s1")
    assert [r.total_value for r in rows] == [1000.0, 1100.0]
    assert rows[1].cumulative_return == pytest.approx(0.10)


def test_daily_pnl_reports_days_elapsed_and_nulls_daily_fields_across_gaps():
    rows = daily_pnl(
        [
            (date(2026, 1, 2), 100.0),  # Friday
            (date(2026, 1, 5), 110.0),  # Monday: a weekend is not a gap
            (date(2026, 1, 20), 121.0),  # two weeks missing
        ]
    )
    assert [r.days_elapsed for r in rows] == [None, 3, 15]
    assert rows[1].daily_change == pytest.approx(10.0)
    assert rows[1].daily_return == pytest.approx(0.10)
    assert rows[2].daily_change is None
    assert rows[2].daily_return is None
    assert rows[2].cumulative_return == pytest.approx(0.21)


def test_daily_pnl_gap_threshold_is_configurable():
    rows = daily_pnl([(date(2026, 1, 1), 100.0), (date(2026, 1, 3), 110.0)], max_gap_days=1)
    assert rows[1].daily_return is None


def _snap_as_of(state, tick_id, as_of, taken_at, value):
    state.execute(
        "INSERT OR IGNORE INTO tick_runs (id, started_at, status) VALUES (?, ?, 'ok')",
        [tick_id, taken_at],
    )
    state.execute(
        "INSERT INTO portfolio_snapshots (tick_id, as_of, taken_at, cash, positions_json,"
        " total_value) VALUES (?, ?, ?, ?, '{}', ?)",
        [tick_id, as_of, taken_at, value, value],
    )


def test_load_pnl_groups_real_snapshots_by_as_of_not_wall_clock(state):
    # Monday's tick ran late, after midnight UTC on Tuesday; then Tuesday's.
    _snap_as_of(state, "t1", "2026-01-05", "2026-01-06T00:30:00+00:00", 100.0)
    _snap_as_of(state, "t2", "2026-01-06", "2026-01-06T22:45:00+00:00", 104.0)
    rows = load_pnl(state)
    assert [(r.day, r.total_value) for r in rows] == [
        (date(2026, 1, 5), 100.0),
        (date(2026, 1, 6), 104.0),
    ]


def test_load_pnl_same_as_of_rerun_keeps_the_latest_row(state):
    _snap_as_of(state, "t1", "2026-01-05", "2026-01-05T22:45:00+00:00", 100.0)
    _snap_as_of(state, "t2", "2026-01-05", "2026-01-05T23:10:00+00:00", 101.0)
    assert [r.total_value for r in load_pnl(state)] == [101.0]


def test_gap_limit_compares_rows_up_to_and_including_the_limit():
    """TT-02: ``elapsed`` is None only on the first row. Later rows compare
    with the previous one while the gap is within the limit."""
    rows = daily_pnl(
        [(date(2026, 1, 1), 100.0), (date(2026, 1, 4), 110.0), (date(2026, 1, 8), 121.0)],
        max_gap_days=3,
    )
    assert rows[0].days_elapsed is None
    assert rows[0].daily_change is None
    assert rows[1].days_elapsed == 3
    assert rows[1].daily_change == pytest.approx(10.0)
    assert rows[2].days_elapsed == 4
    assert rows[2].daily_change is None


# ---- the headline day change (one source for Dashboard and Insights) ----------


def test_day_change_is_the_last_row_against_the_one_before():
    from stonks.production.pnl import day_change

    change = day_change(daily_pnl([(date(2026, 9, 23), 10_150.0), (date(2026, 9, 24), 10_143.91)]))
    assert change is not None
    assert change.day == date(2026, 9, 24)
    assert change.previous_day == date(2026, 9, 23)
    assert change.value == pytest.approx(10_143.91)
    assert change.change == pytest.approx(-6.09)
    assert change.change_pct == pytest.approx(-6.09 / 10_150.0)


def test_day_change_is_empty_across_a_gap_and_before_two_rows():
    from stonks.production.pnl import day_change

    assert day_change([]) is None
    one = day_change(daily_pnl([(date(2026, 9, 24), 100.0)]))
    assert one is not None and one.change is None and one.previous_day is None
    gap = day_change(daily_pnl([(date(2026, 9, 1), 100.0), (date(2026, 9, 24), 90.0)]))
    assert gap is not None and gap.change is None and gap.change_pct is None
