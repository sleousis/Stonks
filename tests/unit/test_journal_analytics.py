"""Journal analytics (roadmap 23.3): results by group, the P&L calendar."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from stonks.journal.analytics import group_stats, pnl_calendar
from stonks.journal.trips import JournalTrade

BASE = JournalTrade(
    leg_id="1.1",
    trade_id=1,
    ticker="X.US",
    side="long",
    sleeve="mom",
    origin="strategy",
    entry_client_id="b",
    exit_client_id="s",
    entry_at=datetime(2026, 3, 2, tzinfo=UTC),
    exit_at=datetime(2026, 3, 4, tzinfo=UTC),
    quantity=1,
    entry_price=100,
    exit_price=110,
    is_open=False,
    holding_days=2.0,
    pnl=10.0,
    return_pct=0.1,
    fees=0.0,
    dividends=0.0,
    mae_pct=-0.01,
    mfe_pct=0.12,
    exit_efficiency=0.8,
    exit_trigger="signal",
    stop_price=None,
    stop_source=None,
    target_price=None,
    risk_amount=None,
    r_multiple=None,
    mae_r=None,
)


def _t(n, pnl, exit_day, **kw):
    return replace(
        BASE,
        leg_id=f"{n}.1",
        trade_id=n,
        pnl=pnl,
        exit_at=datetime(2026, 3, exit_day, 15, tzinfo=UTC),
        **kw,
    )


def test_group_stats_split_wins_losses_r_and_efficiency():
    trades = [
        _t(1, 30.0, 2, sleeve="a", r_multiple=3.0),
        _t(2, -10.0, 3, sleeve="a", r_multiple=-1.0, exit_efficiency=0.2),
        _t(3, 5.0, 3, sleeve="b"),
        replace(_t(4, 99.0, 4, sleeve="a"), is_open=True, exit_at=None),
    ]
    groups = {g.key: g for g in group_stats(trades, lambda t: [t.sleeve])}
    a = groups["a"]
    assert (a.trades, a.wins, a.losses, a.open) == (2, 1, 1, 1)
    assert a.win_rate == pytest.approx(0.5)
    assert a.pnl == pytest.approx(20.0)
    assert a.avg_win == pytest.approx(30.0) and a.avg_loss == pytest.approx(-10.0)
    assert a.profit_factor == pytest.approx(3.0)
    assert a.avg_r == pytest.approx(1.0) and a.r_trades == 2
    assert a.avg_exit_efficiency == pytest.approx(0.5)
    assert groups["b"].avg_r is None
    assert [g.key for g in group_stats(trades, lambda t: [t.sleeve])] == ["a", "b"]


def test_a_trade_counts_once_in_each_of_its_groups():
    trades = [_t(1, 10.0, 2), _t(2, -4.0, 3)]
    labels = {1: ["breakout", "gap"], 2: []}
    groups = {g.key: g for g in group_stats(trades, lambda t: labels[t.trade_id] or ["untagged"])}
    assert set(groups) == {"breakout", "gap", "untagged"}
    assert groups["gap"].pnl == pytest.approx(10.0)
    assert groups["untagged"].losses == 1


def test_calendar_sums_closed_legs_by_exit_day_week_and_month():
    trades = [
        _t(1, 10.0, 2),
        _t(2, -4.0, 2),
        _t(3, 7.0, 9),
        replace(_t(4, 50.0, 1), exit_at=datetime(2026, 4, 1, tzinfo=UTC)),
        replace(_t(5, 99.0, 9), is_open=True, exit_at=None),
    ]
    cal = pnl_calendar(trades, lambda t: t.pnl)
    assert [(d.key, d.pnl, d.trades, d.wins) for d in cal.days] == [
        ("2026-03-02", 6.0, 2, 1),
        ("2026-03-09", 7.0, 1, 1),
        ("2026-04-01", 50.0, 1, 1),
    ]
    assert [(w.key, w.start, w.pnl) for w in cal.weeks] == [
        ("2026-W10", date(2026, 3, 2), 6.0),
        ("2026-W11", date(2026, 3, 9), 7.0),
        ("2026-W14", date(2026, 3, 30), 50.0),
    ]
    assert [(m.key, m.pnl, m.trades) for m in cal.months] == [
        ("2026-03", 13.0, 3),
        ("2026-04", 50.0, 1),
    ]
    assert cal.total == pytest.approx(63.0)
    assert cal.best_day == "2026-04-01" and cal.worst_day == "2026-03-02"


def test_calendar_skips_legs_with_no_amount():
    cal = pnl_calendar(
        [_t(1, 10.0, 2), _t(2, 5.0, 2)], lambda t: None if t.trade_id == 2 else t.pnl
    )
    assert cal.total == pytest.approx(10.0)
    assert cal.days[0].trades == 1
    assert cal.unconverted == 1
