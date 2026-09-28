"""Live risk monitoring end to end over real stores (BL-47, roadmap 9.5.4):
the snapshot rows, the violation scoring across days, the alerts, the tick
hook and the health check."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from stonks.config import HealthConfig
from stonks.production.decay import DecaySettings
from stonks.production.health import check_health
from stonks.production.hooks import TickHookContext, registered_hooks
from stonks.production.risk_metrics import (
    PORTFOLIO_BOOK,
    RiskMonitorSettings,
    latest_portfolio_rows,
    latest_snapshots,
    list_snapshots,
    record_risk_snapshots,
)
from stonks.store.state import SqliteState

PF = "pf_default"
SID = "bah_aaa"
START = date(2025, 1, 1)


def _days(n: int) -> list[date]:
    return [d.date() for d in pd.bdate_range(start=START, periods=n)]


@pytest.fixture
def market(lake):
    """300 weekdays of AAA.US with 1% daily moves, a known sigma."""
    days = _days(300)
    rng = np.random.default_rng(11)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, len(days))))
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": "AAA.US",
                "date": days,
                "open": closes,
                "high": closes,
                "low": closes,
                "close": closes,
                "adj_close": closes,
                "volume": 1_000,
            }
        )
    )
    return lake, days, closes


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    s.execute(
        "INSERT INTO strategies (id, class_path, params_json, artifact_path, status,"
        " created_at, updated_at) VALUES (?, 'x.Y', '{}', '', 'active', ?, ?)",
        [SID, "2025-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00"],
    )
    yield s
    s.close()


def _book(state, day: date, qty: float, cash: float = 1000.0) -> None:
    tick = f"t-{day}"
    state.execute(
        "INSERT INTO tick_runs (id, started_at, status) VALUES (?, ?, 'ok')",
        [tick, f"{day}T21:00:00+00:00"],
    )
    state.execute(
        "INSERT INTO portfolio_snapshots (tick_id, portfolio_id, as_of, taken_at, cash,"
        " positions_json, total_value) VALUES (?, ?, ?, ?, ?, ?, 0)",
        [tick, PF, day.isoformat(), f"{day}T21:00:00+00:00", cash, json.dumps({"AAA.US": qty})],
    )
    state.execute(
        "INSERT INTO position_attribution (tick_id, portfolio_id, as_of, ticker, strategy_id,"
        " quantity, weight_share, source, created_at) VALUES (?, ?, ?, 'AAA.US', ?, ?, 1.0,"
        " 'decision', ?)",
        [tick, PF, day.isoformat(), SID, qty, f"{day}T21:00:00+00:00"],
    )


def test_one_row_per_book_with_a_known_sigma_forecast(state, market):
    lake, days, closes = market
    day = days[-1]
    _book(state, day, 10.0)
    snaps, alerts = record_risk_snapshots(state, lake, day, [PF], tick_id="t1")
    assert alerts == []
    [pf, sleeve] = sorted(snaps, key=lambda s: s.strategy_id)
    assert pf.strategy_id == PORTFOLIO_BOOK and sleeve.strategy_id == SID
    exposure = 10.0 * closes[-1]
    assert pf.value == pytest.approx(1000.0 + exposure)
    assert sleeve.value == pytest.approx(exposure)
    # a sleeve fully in one name carries that name's sigma; cash dilutes the book
    assert sleeve.sigma == pytest.approx(0.01, rel=0.4)
    assert pf.var_95 == pytest.approx(sleeve.var_95 * exposure / pf.value)
    assert pf.observations == 250
    assert pf.realized_return is None and pf.violation_95 is None
    rows, total = list_snapshots(state, PF)
    assert total == 2 and {r.strategy_id for r in rows} == {"", SID}


def test_a_rerun_of_the_same_day_replaces_the_row(state, market):
    lake, days, _ = market
    _book(state, days[-1], 10.0)
    record_risk_snapshots(state, lake, days[-1], [PF])
    record_risk_snapshots(state, lake, days[-1], [PF])
    assert list_snapshots(state, PF)[1] == 2


def test_violations_are_scored_against_the_previous_forecast(state, market):
    lake, days, closes = market
    scored = days[-80:]
    for day in scored:
        _book(state, day, 10.0, cash=0.0)
        record_risk_snapshots(state, lake, day, [PF])
    rows, _ = list_snapshots(state, PF, strategy_id=PORTFOLIO_BOOK, limit=500)
    rows = sorted(rows, key=lambda r: r.as_of)
    # day two onward: the hypothetical return of yesterday's book
    ret = closes[-79] / closes[-80] - 1
    assert rows[1].realized_return == pytest.approx(ret)
    assert rows[1].violation_95 == (ret < -rows[0].var_95)
    last = rows[-1]
    assert last.window_days == 79
    flags = [r.violation_95 for r in rows[1:]]
    assert last.violations_95 == sum(flags)
    assert last.violation_ratio_95 == pytest.approx(sum(flags) / (0.05 * 79))
    assert 0.0 <= (last.kupiec_p_95 or 0.0) <= 1.0
    [latest] = latest_portfolio_rows(state)
    assert latest.as_of == scored[-1]
    assert {s.strategy_id for s in latest_snapshots(state, PF)} == {"", SID}


def test_an_out_of_band_ratio_alerts_the_owner_once(state, market, monkeypatch):
    lake, days, _ = market
    events = []
    # a model that sees no risk: every loss is a violation
    import stonks.production.risk_metrics as rm

    monkeypatch.setattr(rm, "ewma_sigma", lambda *a, **k: 1e-6)
    settings = RiskMonitorSettings()
    for day in days[-80:]:
        _book(state, day, 10.0, cash=0.0)
        record_risk_snapshots(state, lake, day, [PF], settings=settings, publish=events.append)
    var_alerts = [e for e in events if e.title.startswith("VaR model off")]
    # the book and the sleeve each cross the band once
    assert len(var_alerts) == 2
    assert var_alerts[0].audience.kind == "owner" and var_alerts[0].category == "risk"
    health = check_health(state, lake, [], HealthConfig(), now=datetime(2027, 1, 1, tzinfo=UTC))
    check = {c.name: c for c in health.checks}["var_violations"]
    assert check.ok is False and PF in check.detail


def test_health_passes_without_enough_scored_days(state, market):
    lake, days, _ = market
    _book(state, days[-1], 10.0)
    record_risk_snapshots(state, lake, days[-1], [PF])
    health = check_health(state, lake, [], HealthConfig(), now=datetime(2027, 1, 1, tzinfo=UTC))
    check = {c.name: c for c in health.checks}["var_violations"]
    # Passing for lack of data says so, so the console can show "Not enough data yet".
    assert check.ok is True and check.detail == "not enough days yet"


def test_health_says_the_ratio_is_in_the_band_once_judged(state, market):
    lake, days, _ = market
    for day in days[-80:]:
        _book(state, day, 10.0, cash=0.0)
        record_risk_snapshots(state, lake, day, [PF])
    health = check_health(state, lake, [], HealthConfig(), now=datetime(2027, 1, 1, tzinfo=UTC))
    check = {c.name: c for c in health.checks}["var_violations"]
    assert check.ok is True and check.detail == "within band"


def test_decay_alerts_when_the_sleeve_loses_money_for_weeks(state, lake):
    days = _days(200)
    falling = 100.0 * np.exp(np.cumsum(np.full(len(days), -0.004)))
    falling = falling * (1 + np.random.default_rng(3).normal(0, 0.002, len(days)))
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": "AAA.US",
                "date": days,
                "open": falling,
                "high": falling,
                "low": falling,
                "close": falling,
                "adj_close": falling,
                "volume": 1_000,
            }
        )
    )
    events = []
    decay = DecaySettings(short_window=10, long_window=20, negative_days=5)
    for day in days[-30:]:
        _book(state, day, 10.0)
        record_risk_snapshots(state, lake, day, [PF], decay=decay, publish=events.append)
    decayed = [e for e in events if e.title.startswith("Alpha decay")]
    assert len(decayed) == 1 and decayed[0].strategy_id == SID
    [sleeve] = [s for s in latest_snapshots(state, PF) if s.strategy_id == SID]
    assert sleeve.decayed and sleeve.ir_short is not None and sleeve.ir_short < 0


def test_the_hook_records_real_ticks_and_skips_dry_runs(state, market):
    lake, days, _ = market
    [hook] = [h for h in registered_hooks("tick") if h.name == "risk_monitor"]
    _book(state, days[-1], 10.0)

    def ctx(dry_run: bool) -> TickHookContext:
        return TickHookContext(
            state=state,
            lake=lake,
            tick_id="t-hook",
            as_of=days[-1],
            dry_run=dry_run,
            signals=None,
            portfolios={PF: {}},
        )

    assert hook.run(ctx(True)) is None
    assert list_snapshots(state, PF)[1] == 0
    hook.run(ctx(False))
    rows, total = list_snapshots(state, PF)
    assert total == 2 and all(r.tick_id == "t-hook" for r in rows)


def test_nothing_is_recorded_without_the_table(tmp_path, market):
    lake, days, _ = market
    with SqliteState(tmp_path / "old.sqlite") as old:
        assert record_risk_snapshots(old, lake, days[-1], [PF]) == ([], [])


def test_a_portfolio_without_a_snapshot_that_day_is_skipped(state, market):
    lake, days, _ = market
    _book(state, days[-2], 10.0)
    snaps, _ = record_risk_snapshots(state, lake, days[-1], [PF])
    assert snaps == []
    assert (days[-1] - timedelta(days=1)) >= days[-2]
