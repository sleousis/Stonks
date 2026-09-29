"""The behaviour report on manual and synced trades (roadmap 23.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stonks.insights.behaviour import BehaviourFill, BehaviourSettings, behaviour_report

T0 = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)  # a Monday


def _f(i: int, side: str, qty: float, price: float, at: datetime, ticker: str = "UP.US",
       source: str = "manual") -> BehaviourFill:  # fmt: skip
    return BehaviourFill(
        id=f"f{i}", ticker=ticker, side=side, quantity=qty, price=price, fee=0.0,  # type: ignore[arg-type]
        filled_at=at, source=source,  # type: ignore[arg-type]
    )  # fmt: skip


def _trips():
    return [
        # a winner held 2 days, entered Monday
        _f(1, "buy", 10, 100.0, T0),
        _f(2, "sell", 10, 110.0, T0 + timedelta(days=2)),
        # a loser held 20 days, entered Wednesday
        _f(3, "buy", 10, 50.0, T0 + timedelta(days=2), ticker="DN.US"),
        _f(4, "sell", 10, 40.0, T0 + timedelta(days=22), ticker="DN.US"),
        # a revenge trade an hour after that losing exit, a small loser
        _f(5, "buy", 5, 40.0, T0 + timedelta(days=22, hours=1), ticker="DN.US"),
        _f(6, "sell", 5, 39.0, T0 + timedelta(days=23), ticker="DN.US", source="broker"),
    ]


def test_win_rate_and_totals():
    r = behaviour_report(_trips())
    assert r.trades == 3
    assert r.win_rate == pytest.approx(1 / 3)
    assert r.total_pnl == pytest.approx(100.0 - 100.0 - 5.0)
    assert r.open_positions == 0


def test_by_holding_time_and_weekday():
    r = behaviour_report(_trips())
    buckets = {b.label: b for b in r.by_holding}
    assert buckets["1 to 5 days"].trades == 1 and buckets["under a day"].trades == 1
    assert buckets["1 to 4 weeks"].pnl == pytest.approx(-100.0)
    days = {d.label: d for d in r.by_weekday}
    assert days["Mon"].trades == 1 and days["Mon"].pnl == pytest.approx(100.0)
    assert days["Wed"].trades == 1


def test_disposition_effect_when_losers_are_held_longer():
    r = behaviour_report(_trips())
    d = r.disposition
    assert d.avg_days_winners == pytest.approx(2.0)
    assert d.avg_days_losers > d.avg_days_winners
    assert d.present is True


def test_revenge_trades_after_a_loss():
    r = behaviour_report(_trips(), BehaviourSettings(revenge_hours=4))
    assert r.revenge.trades == 1
    assert r.revenge.pnl == pytest.approx(-5.0)
    assert r.revenge.win_rate == 0.0


def test_overtrading_flags_busy_days():
    fills = []
    for i in range(6):
        at = T0 + timedelta(minutes=i * 10)
        fills += [
            _f(2 * i, "buy", 1, 100.0, at),
            _f(2 * i + 1, "sell", 1, 99.0, at + timedelta(minutes=5)),
        ]
    r = behaviour_report(fills, BehaviourSettings(busy_day_entries=5))
    assert r.overtrading.busy_days == 1
    assert r.overtrading.busy_day_pnl == pytest.approx(-6.0)
    assert r.overtrading.max_entries_in_a_day == 6


def test_trading_against_strategies_costs():
    def stance(ticker, day, direction):
        return "against" if ticker == "DN.US" else "with"

    r = behaviour_report(_trips(), stance=stance)
    groups = {g.label: g for g in r.versus_strategies}
    assert groups["against"].trades == 2
    assert groups["against"].pnl == pytest.approx(-105.0)
    assert groups["with"].pnl == pytest.approx(100.0)
    assert r.against_strategies_cost == pytest.approx(-105.0)


def test_sources_are_counted_and_empty_is_fine():
    r = behaviour_report(_trips())
    assert r.sources == {"manual": 5, "broker": 1}
    empty = behaviour_report([])
    assert empty.trades == 0 and empty.win_rate is None


def test_a_split_between_entry_and_exit_rescales_the_open_lot():
    """10 shares bought at 100, a 2:1 split, 20 sold at 55: one winning
    trip of 20 shares from 50, not half a trip and a phantom short."""
    from datetime import date

    from stonks.core.corporate_actions import Split

    fills = [_f(1, "buy", 10, 100.0, T0), _f(2, "sell", 20, 55.0, T0 + timedelta(days=10))]
    r = behaviour_report(fills, splits=[Split("UP.US", date(2026, 3, 5), 2.0)])
    assert r.trades == 1 and r.open_positions == 0
    assert r.total_pnl == pytest.approx(20 * (55.0 - 50.0))
