"""Integration tests for the production tick."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.protocols import SurvivalReport
from stonks.production.tick import TickSettings, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold


@pytest.fixture
def tick_env(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")

    registry.set_status(
        registry.register(
            BuyAndHold({"ticker": "UP.US", "allocation": 1.0}),
            reports=[SurvivalReport(test_id="oos", passed=True, metrics={"sharpe_oos": 1.0})],
        ),
        "active",
    )
    yield lake_trending, state, registry
    state.close()


def test_tick_places_order_and_records_everything(tick_env):
    lake, state, registry = tick_env
    settings = TickSettings(universe=["UP.US", "DOWN.US"], threshold=0.0, initial_cash=10_000.0)
    result = run_tick(
        state=state,
        lake=lake,
        registry=registry,
        settings=settings,
        as_of=date(2026, 3, 20),
        dry_run=False,
    )

    assert result.status in ("ok", "partial")
    assert result.orders_placed == 1
    assert result.fills == 1

    tick_rows = state.sql("SELECT * FROM tick_runs WHERE id = ?", [result.tick_id])
    assert len(tick_rows) == 1
    assert tick_rows[0]["status"] in ("ok", "partial")

    order_rows = state.sql("SELECT * FROM orders WHERE tick_id = ?", [result.tick_id])
    assert len(order_rows) == 1
    fill_rows = state.sql(
        "SELECT * FROM fills WHERE order_client_id = ?", [order_rows[0]["client_id"]]
    )
    assert len(fill_rows) == 1

    snaps = state.sql("SELECT * FROM portfolio_snapshots WHERE tick_id = ?", [result.tick_id])
    assert len(snaps) == 1


def test_tick_is_idempotent_on_same_day(tick_env):
    lake, state, registry = tick_env
    settings = TickSettings(universe=["UP.US"], threshold=0.0, initial_cash=10_000.0)
    r1 = run_tick(state, lake, registry, settings, as_of=date(2026, 3, 20))
    r2 = run_tick(state, lake, registry, settings, as_of=date(2026, 3, 20))

    # second tick re-submits same client_id; broker short-circuits duplicates.
    # State tracks both tick_runs, but orders/fills for the second tick
    # reuse the first's client_id (no duplicate rows).
    orders = state.sql("SELECT DISTINCT client_id FROM orders")
    assert len(orders) == 1
    _ = r1, r2


def test_tick_dry_run_records_no_orders_or_snapshots(tick_env):
    lake, state, registry = tick_env
    settings = TickSettings(universe=["UP.US"], threshold=0.0, initial_cash=10_000.0)
    result = run_tick(state, lake, registry, settings, as_of=date(2026, 3, 20), dry_run=True)

    assert result.orders_placed >= 1  # decisions were ranked, but...
    assert state.count_rows("orders") == 0
    assert state.count_rows("fills") == 0
    assert state.count_rows("portfolio_snapshots") == 0
    # tick_runs ledger still closed
    runs = state.sql("SELECT status FROM tick_runs WHERE id = ?", [result.tick_id])
    assert runs[0]["status"] in ("ok", "noop")


def test_tick_with_no_active_strategies_is_noop(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    try:
        registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
        settings = TickSettings(universe=["UP.US"], threshold=0.0, initial_cash=10_000.0)
        result = run_tick(state, lake_trending, registry, settings, as_of=date(2026, 3, 20))
        assert result.status == "noop"
        assert result.orders_placed == 0
    finally:
        state.close()
