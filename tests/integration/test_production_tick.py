"""Integration tests for the production tick."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.protocols import SurvivalReport
from stonks.production.tick import TickSettings, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status


@pytest.fixture
def tick_env(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")

    seed_status(
        registry,
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


def test_rerun_after_crash_before_snapshot_does_not_resubmit_filled_orders(tick_env):
    """A tick that crashed after recording its fill but before the snapshot
    is re-run for the same ``as_of``: the already-filled order must not be
    submitted again, and its fill must not be applied a second time."""
    lake, state, registry = tick_env
    settings = TickSettings(universe=["UP.US"], threshold=0.0, initial_cash=10_000.0)
    r1 = run_tick(state, lake, registry, settings, as_of=date(2026, 3, 20))
    assert r1.fills == 1
    # Simulate the crash: the snapshot never landed.
    state.execute("DELETE FROM portfolio_snapshots")

    r2 = run_tick(state, lake, registry, settings, as_of=date(2026, 3, 20))

    assert r2.tick_id != r1.tick_id  # tick_runs ids stay unique per run
    assert state.count_rows("tick_runs") == 2
    assert state.count_rows("orders") == 1
    assert state.count_rows("fills") == 1
    assert r2.fills == 0


def test_crash_before_snapshot_rolls_back_orders_and_fills_and_marks_tick_error(
    tick_env, monkeypatch
):
    import json

    import stonks.production.tick as tick_mod

    lake, state, registry = tick_env
    settings = TickSettings(universe=["UP.US"], threshold=0.0, initial_cash=10_000.0)

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(tick_mod, "_snapshot_portfolio", boom)

    with pytest.raises(RuntimeError, match="disk full"):
        run_tick(state, lake, registry, settings, as_of=date(2026, 3, 20))

    # orders, fills and snapshot commit together or not at all
    assert state.count_rows("orders") == 0
    assert state.count_rows("fills") == 0
    assert state.count_rows("portfolio_snapshots") == 0

    runs = state.sql("SELECT status, finished_at, summary_json FROM tick_runs")
    assert len(runs) == 1
    assert runs[0]["status"] == "error"
    assert runs[0]["finished_at"] is not None
    assert "disk full" in json.loads(runs[0]["summary_json"])["error"]


def test_utc_today_is_the_utc_date_of_the_clock():
    """A pinned clock (TT-06): 23:30 in New York is already the next day in UTC."""
    from datetime import datetime, timedelta, timezone

    from stonks.production.tick import utc_today

    new_york = timezone(timedelta(hours=-5))
    assert utc_today(lambda: datetime(2026, 3, 20, 23, 30, tzinfo=new_york)) == date(2026, 3, 21)


def test_default_as_of_is_the_utc_date(tick_env, monkeypatch):
    import stonks.production.tick as tick_mod

    lake, state, registry = tick_env
    monkeypatch.setattr(tick_mod, "utc_today", lambda: date(2026, 3, 20))
    settings = TickSettings(universe=["UP.US"], threshold=0.0, initial_cash=10_000.0)
    result = run_tick(state, lake, registry, settings, as_of=None)
    assert result.tick_id.startswith("tick_2026-03-20_")


def test_tick_settings_default_price_staleness_is_seven_days():
    assert TickSettings(universe=["UP.US"]).max_price_staleness_days == 7


def test_tick_does_not_trade_on_months_old_prices(tick_env):
    lake, state, registry = tick_env
    settings = TickSettings(universe=["UP.US"], threshold=0.0, initial_cash=10_000.0)
    result = run_tick(state, lake, registry, settings, as_of=date(2026, 9, 1))

    assert result.fills == 0
    assert state.count_rows("fills") == 0


def test_crash_during_rank_marks_tick_error(tick_env, monkeypatch):
    from stonks.production.ranker import Ranker

    lake, state, registry = tick_env
    settings = TickSettings(universe=["UP.US"], threshold=0.0, initial_cash=10_000.0)

    def boom(self, as_of):
        raise RuntimeError("lake exploded")

    monkeypatch.setattr(Ranker, "rank", boom)

    with pytest.raises(RuntimeError, match="lake exploded"):
        run_tick(state, lake, registry, settings, as_of=date(2026, 3, 20))

    runs = state.sql("SELECT status FROM tick_runs")
    assert [r["status"] for r in runs] == ["error"]


def _summary(state, tick_id: str) -> dict:
    import json

    rows = state.sql("SELECT summary_json FROM tick_runs WHERE id = ?", [tick_id])
    return json.loads(rows[0]["summary_json"])


def test_tick_summary_records_dry_run_and_broker_mode(tick_env):
    lake, state, registry = tick_env
    settings = TickSettings(universe=["UP.US"], threshold=0.0, initial_cash=10_000.0)

    dry = run_tick(state, lake, registry, settings, as_of=date(2026, 3, 20), dry_run=True)
    real = run_tick(state, lake, registry, settings, as_of=date(2026, 3, 20))

    assert (dry.dry_run, dry.broker_mode) == (True, "simulated")
    assert (real.dry_run, real.broker_mode) == (False, "simulated")
    summary = _summary(state, dry.tick_id)
    assert (summary["dry_run"], summary["broker_mode"]) == (True, "simulated")
    summary = _summary(state, real.tick_id)
    assert (summary["dry_run"], summary["broker_mode"]) == (False, "simulated")


def test_tick_summary_names_the_configured_broker_mode(tick_env):
    lake, state, registry = tick_env
    settings = TickSettings(
        universe=["UP.US"], threshold=0.0, broker_kind="alpaca", broker_mode="paper"
    )
    # no broker factory: the tick fails, and its row still names the mode
    with pytest.raises(ValueError, match="broker_factory"):
        run_tick(state, lake, registry, settings, as_of=date(2026, 3, 20), dry_run=True)
    [row] = state.sql("SELECT id FROM tick_runs")
    assert _summary(state, row["id"])["broker_mode"] == "paper"


def test_a_failed_tick_still_records_dry_run_and_broker_mode(tick_env, monkeypatch):
    from stonks.production.ranker import Ranker

    lake, state, registry = tick_env
    settings = TickSettings(universe=["UP.US"], threshold=0.0, initial_cash=10_000.0)

    def boom(self, as_of):
        raise RuntimeError("lake exploded")

    monkeypatch.setattr(Ranker, "rank", boom)
    with pytest.raises(RuntimeError):
        run_tick(state, lake, registry, settings, as_of=date(2026, 3, 20), dry_run=True)

    [row] = state.sql("SELECT id FROM tick_runs")
    summary = _summary(state, row["id"])
    assert (summary["dry_run"], summary["broker_mode"]) == (True, "simulated")


def test_tick_settings_refuse_a_broker_mode_that_contradicts_the_broker():
    with pytest.raises(ValueError, match="broker_mode"):
        TickSettings(universe=["UP.US"], broker_mode="live")
    with pytest.raises(ValueError, match="broker_mode"):
        TickSettings(universe=["UP.US"], broker_kind="alpaca", broker_mode="simulated")


def test_an_external_broker_without_a_mode_counts_as_live():
    """Unknown means real money: the label never understates the risk."""
    assert TickSettings(universe=["UP.US"], broker_kind="alpaca").broker_mode == "live"
    assert TickSettings(universe=["UP.US"]).broker_mode == "simulated"
