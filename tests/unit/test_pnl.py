"""Daily P&L from portfolio snapshots (roadmap 2.5c)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from stonks.production.pnl import daily_pnl, load_pnl
from stonks.store.state import SqliteState


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


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


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
