"""portfolio_snapshots.as_of (migration 004) and the backdated-tick guard."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from stonks.core.protocols import SurvivalReport
from stonks.production.tick import BackdatedTickError, TickSettings, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import MIGRATIONS_DIR, SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold

SETTINGS = TickSettings(universe=["UP.US"], threshold=0.0, initial_cash=10_000.0)


@pytest.fixture
def tick_env(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    registry.set_status(
        registry.register(
            BuyAndHold({"ticker": "UP.US", "allocation": 1.0}),
            reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
        ),
        "active",
    )
    yield lake_trending, state, registry
    state.close()


def test_migration_004_backfills_as_of_from_taken_at(tmp_path):
    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for sql in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(sql.stem.split("_", 1)[0])
        if version >= 4:
            continue
        con.executescript(sql.read_text())
        con.execute("INSERT INTO schema_migrations VALUES (?, 'x')", [version])
    con.execute("INSERT INTO tick_runs (id, started_at, status) VALUES ('t0', 'x', 'ok')")
    con.execute(
        "INSERT INTO portfolio_snapshots (tick_id, taken_at, cash, positions_json, total_value)"
        " VALUES ('t0', '2026-03-20T22:45:01+00:00', 1, '{}', 1)"
    )
    con.commit()
    con.close()

    state = SqliteState(path)
    try:
        state.migrate()
        rows = state.sql("SELECT as_of FROM portfolio_snapshots")
        assert [r["as_of"] for r in rows] == ["2026-03-20"]
    finally:
        state.close()


def test_snapshot_records_the_tick_as_of(tick_env):
    lake, state, registry = tick_env
    run_tick(state, lake, registry, SETTINGS, as_of=date(2026, 3, 20))
    rows = state.sql("SELECT as_of FROM portfolio_snapshots")
    assert [r["as_of"] for r in rows] == ["2026-03-20"]


def test_backdated_tick_is_refused_and_writes_nothing(tick_env):
    lake, state, registry = tick_env
    run_tick(state, lake, registry, SETTINGS, as_of=date(2026, 3, 20))
    before = {t: state.count_rows(t) for t in ("tick_runs", "orders", "portfolio_snapshots")}

    with pytest.raises(BackdatedTickError, match="2026-03-20"):
        run_tick(state, lake, registry, SETTINGS, as_of=date(2026, 3, 19))

    after = {t: state.count_rows(t) for t in ("tick_runs", "orders", "portfolio_snapshots")}
    assert after == before


def test_same_day_rerun_and_backdated_dry_run_are_allowed(tick_env):
    lake, state, registry = tick_env
    run_tick(state, lake, registry, SETTINGS, as_of=date(2026, 3, 20))
    run_tick(state, lake, registry, SETTINGS, as_of=date(2026, 3, 20))
    result = run_tick(state, lake, registry, SETTINGS, as_of=date(2026, 3, 19), dry_run=True)
    assert result.status in ("ok", "noop")
