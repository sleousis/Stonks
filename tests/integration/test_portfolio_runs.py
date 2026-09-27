"""``portfolio_runs``: one row per portfolio per tick, and the paper-day
count of the auto gate read from it (decision 2026-09-26)."""

from __future__ import annotations

from datetime import date

from stonks.accounts.paper import paper_days_completed
from stonks.production.portfolio_runs import PortfolioRun, list_runs, record_run


def _run(day: int, *, subs=("sub_a",), status="ok", breached=False, tick=None, **kw):
    return PortfolioRun(
        tick_id=tick or f"tick_{day}",
        portfolio_id="pf_default",
        as_of=date(2026, 1, day),
        mode="paper",
        status=status,
        risk_breached=breached,
        paper_subscriptions=tuple(subs),
        started_at=f"2026-01-{day:02d}T21:00:00+00:00",
        finished_at=f"2026-01-{day:02d}T21:00:05+00:00",
        **kw,
    )


def test_a_run_round_trips_and_a_rerun_of_the_same_tick_replaces_it(state):
    record_run(state, _run(2, orders_placed=1, fills=1))
    record_run(state, _run(2, orders_placed=2, fills=2))
    [row] = list_runs(state, "pf_default")
    assert row.orders_placed == 2 and row.fills == 2
    assert row.paper_subscriptions == ("sub_a",) and row.as_of == date(2026, 1, 2)


def test_paper_days_count_distinct_trading_days(state):
    for day in (2, 5, 6):  # Fri, Mon, Tue
        record_run(state, _run(day))
    record_run(state, _run(6, tick="tick_6_rerun"))  # a same-day re-run
    record_run(state, _run(7, subs=("sub_b",)))  # someone else's day
    assert paper_days_completed(state, "sub_a") == 3


def test_errors_do_not_count_and_a_breach_restarts_the_count(state):
    record_run(state, _run(2))
    record_run(state, _run(3, status="error"))
    assert paper_days_completed(state, "sub_a") == 1
    record_run(state, _run(4, breached=True))
    assert paper_days_completed(state, "sub_a") == 0
    record_run(state, _run(5, status="noop"))
    assert paper_days_completed(state, "sub_a") == 1


def test_runs_before_a_reset_do_not_count(state):
    for day in (5, 6, 7):
        record_run(state, _run(day))
    assert paper_days_completed(state, "sub_a", since="2026-01-06T22:00:00+00:00") == 1


# ---- BE-27: paper days cannot be gamed ------------------------------------------------------


def test_be27_weekend_future_and_fully_halted_runs_are_not_paper_days(state):
    record_run(state, _run(2))  # Fri: counts
    record_run(state, _run(3))  # Sat: no session
    record_run(state, _run(5, halted="all"))  # Mon, halted: nothing traded
    record_run(state, _run(6, halted="buys"))  # Tue, reduce-only: still a day
    future = PortfolioRun(
        tick_id="tick_future",
        portfolio_id="pf_default",
        as_of=date(2099, 1, 5),
        mode="paper",
        status="ok",
        paper_subscriptions=("sub_a",),
        started_at="2026-01-07T21:00:00+00:00",
        finished_at="2026-01-07T21:00:05+00:00",
    )
    record_run(state, future)
    assert paper_days_completed(state, "sub_a") == 2
