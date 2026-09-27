"""The ``live_stops`` job (roadmap 19.10): after the opening auction, the
entries that just filled get their protective stops without waiting for
the evening tick. It skips when no live book turns stops on, reconciles
first, and a halt of new orders pauses it."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

import tests.integration.test_tick_modes as modes
from stonks.accounts import PortfolioRepository, Scope
from stonks.config import Settings
from stonks.execution.order_state import mark_unknown
from stonks.production.halts import trip_halt
from stonks.production.live.stops import live_books_of, sync_live_books
from stonks.production.risk import RiskPolicy
from stonks.production.rules.settings import RuleSettings
from stonks.production.tick import load_tick_plan, run_tick
from stonks.scheduling.api_backend import API_ACTIONS
from stonks.scheduling.in_process import IN_PROCESS_ACTIONS
from stonks.scheduling.jobs import JobSpec, RunContext
from stonks.scheduling.local import LOCAL_ACTIONS
from stonks.scheduling.triggers import Fire, SessionTrigger
from stonks.store.state import SqliteState

DAY1, DAY2 = modes.DAY1, modes.DAY2
ON = RiskPolicy(rules=RuleSettings.model_validate({"protective_stops": {"enabled": True}}))
SETTINGS_ON = replace(modes.SETTINGS, risk=ON)
AT = datetime(2026, 3, 18, 14, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_fakes():
    modes.fake.FAKE_BOOKS.clear()
    modes.reset_limiters()
    yield
    modes.fake.FAKE_BOOKS.clear()
    modes.reset_limiters()


@pytest.fixture
def world(tmp_path, lake_trending):
    w = modes.World(tmp_path, lake_trending)
    yield w
    w.state.close()


def _ctx(settings) -> RunContext:
    return RunContext(
        spec=JobSpec("live_stops", "live_stops", SessionTrigger("XNYS", "open", timedelta(0))),
        fire=Fire(AT, date(2026, 3, 18), "k"),
        run_id="srun_t",
        now=AT,
        settings=settings,
        notifier=None,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("registry", [LOCAL_ACTIONS, API_ACTIONS, IN_PROCESS_ACTIONS])
def test_the_job_skips_while_no_live_book_uses_stops(tmp_path, registry):
    settings = Settings(state={"path": tmp_path / "state.sqlite"},
                        lake={"path": tmp_path / "lake.duckdb"}, notify={"backends": []})  # fmt: skip
    with SqliteState(settings.state.path) as state:
        state.migrate()
    out = registry.get("live_stops")(_ctx(settings))
    assert out.status == "skipped" and out.detail["reason"] == "no_live_stops"


def _open(world):
    def open_broker(portfolio_id: str):
        account = PortfolioRepository(world.state).get(Scope.service("t"), portfolio_id)
        return world.traders(account)

    return open_broker


def _sync(world, settings=SETTINGS_ON):
    plan = load_tick_plan(world.state, settings, traders=world.traders, dry_run=True)
    books = live_books_of(plan, settings)
    return sync_live_books(world.state, world.lake, books, _open(world), as_of=DAY2)


def _tick_without_stops(world):
    plan = load_tick_plan(world.state, modes.SETTINGS, traders=world.traders)
    run_tick(world.state, world.lake, world.registry, modes.SETTINGS, as_of=DAY1, plan=plan)


def test_a_filled_entry_gets_its_stop_before_the_evening_tick(world):
    _tick_without_stops(world)
    assert world.book.resting == {}
    result = _sync(world)
    assert set(result) == {world.live}  # only the live book, never the simulated ones
    assert result[world.live]["placed"] == 1
    [cid] = world.book.resting
    assert cid == f"2026-03-17:{world.live}:bh_up:UP.US:buy:stop"
    # a second run keeps it
    assert _sync(world)[world.live] == {"placed": 0, "cancelled": 0, "kept": 1}


def test_stops_off_everywhere_touches_nothing(world):
    _tick_without_stops(world)
    assert _sync(world, settings=modes.SETTINGS) == {}


def test_a_halt_of_new_orders_pauses_it(world):
    _tick_without_stops(world)
    trip_halt(world.state, "kill", reason="drill", actor="t", portfolio_id=world.live,
              halt="all", on=DAY1)  # fmt: skip
    assert _sync(world)[world.live] == {"paused": "a halt holds new orders"}
    assert world.book.resting == {}


def test_an_unknown_order_blocks_the_sync(world):
    _tick_without_stops(world)
    [row] = world.state.sql("SELECT client_id FROM orders WHERE portfolio_id = ?", [world.live])
    world.state.execute("UPDATE orders SET state = 'accepted', status = 'pending'"
                        " WHERE client_id = ?", [row["client_id"]])  # fmt: skip
    world.book.orders.pop(row["client_id"])  # the broker lost track of it
    mark_unknown(world.state, row["client_id"], "submit timed out")
    assert _sync(world)[world.live] == {"skipped": "orders_unreconciled"}
    assert world.book.resting == {}
