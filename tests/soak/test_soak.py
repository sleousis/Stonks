"""The paper soak test (roadmap 12.10): the scheduler drives ingest, ticks,
health and backups over many simulated days on the simulated broker, with
crashes mid-tick, restarts, a kill switch and a DST switch, and the ledger
invariants are checked after every tick (see ``harness.py``).

The smoke run (a few days across the March 2026 DST switch, one crash and
one kill switch day) is part of the default suite. The long run is behind
the ``soak`` marker: ``uv run pytest -m soak tests/soak``."""

from __future__ import annotations

from datetime import date, time

import pytest

from stonks.store.state import SqliteState
from tests.soak.harness import SoakRun, check_day


def test_soak_smoke_across_the_dst_switch(tmp_path, monkeypatch):
    # Thu 5 Mar to Tue 10 Mar 2026: US clocks go forward on Sunday 8 March.
    run = SoakRun(
        tmp_path,
        date(2026, 3, 5),
        date(2026, 3, 10),
        monkeypatch.setattr,
        crash_on=(date(2026, 3, 6),),
        kill_on=date(2026, 3, 9),
    )
    report = run.run()
    assert report.violations == [], report.summary()
    assert report.sessions == [date(2026, 3, d) for d in (5, 6, 9, 10)]
    assert report.restarts == 1
    assert report.halted_days == [date(2026, 3, 9)]
    # the same New York time on both sides of the switch
    assert report.tick_local_times == {time(16, 45)}
    # the churn strategy trades every day except under the kill switch. A
    # paper order fills at the next open (P21): the 3/5 order fills on 3/6,
    # the kill switch cancels the 3/6 order before the 3/9 open, and the
    # 3/10 order is still working
    assert (report.orders, report.fills) == (3, 1)
    assert report.backups >= 4 and report.health_runs >= 8
    assert report.risk_rows >= len(report.sessions)

    # the checks bite: break the ledger and each invariant reports it
    last = report.sessions[-1]
    with SqliteState(run.settings.state.path) as state:
        state.execute(
            "UPDATE portfolio_snapshots SET total_value = total_value + 1 WHERE id ="
            " (SELECT MAX(id) FROM portfolio_snapshots)"
        )
        state.execute(
            "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at,"
            " portfolio_id) SELECT order_client_id, ticker, quantity, price, fee, filled_at,"
            " portfolio_id FROM fills LIMIT 1"
        )
        state.execute(
            "INSERT INTO tick_runs (id, started_at, status) VALUES ('t_stuck', ?, 'running')",
            [f"{last}T21:00:00+00:00"],
        )
    found = " | ".join(check_day(run.settings, last))
    for needle in ("!= equity", "booked 2 times", "fills net to", "stuck in running"):
        assert needle in found, found


@pytest.mark.soak
def test_soak_one_quarter(tmp_path, monkeypatch):
    # February to April 2026: 62 sessions with Presidents' Day, the DST
    # switch and Good Friday, three crashes and a kill switch drill.
    run = SoakRun(
        tmp_path,
        date(2026, 2, 2),
        date(2026, 4, 30),
        monkeypatch.setattr,
        crash_on=(date(2026, 2, 11), date(2026, 3, 9), date(2026, 4, 14)),
        kill_on=date(2026, 3, 20),
    )
    report = run.run()
    print(report.summary())  # noqa: T201 - the soak's result line in the CI log
    assert report.violations == [], "\n".join(report.violations[:50])
    assert len(report.sessions) == 62
    assert date(2026, 2, 16) not in report.sessions  # Presidents' Day
    assert date(2026, 4, 3) not in report.sessions  # Good Friday
    assert report.restarts == 3
    # every order fills at the next open (P21) but two: the one the kill
    # switch cancels and the last session's, still working
    assert (report.orders, report.fills) == (61, 59)
    assert report.tick_local_times == {time(16, 45)}
