"""Financing in the tick (roadmap 16.1 follow-up): a short book's paper
broker is built fresh each tick, so the tick hands ``broker.accrue`` the
last accrual date from the state store. Borrow fees then cover every
calendar day since the last accrual, across restarts and noop days."""

from __future__ import annotations

import json
from datetime import date

import pytest

from stonks.core.protocols import SurvivalReport
from stonks.production.financing import last_accrual
from stonks.production.tick import TickSettings, load_tick_plan, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status
from tests.integration.test_tick_portfolios import People

UNIVERSE = ["UP.US", "FLAT.US", "DOWN.US"]
SETTINGS = TickSettings(universe=UNIVERSE, initial_cash=10_000.0)
DAY_1 = date(2026, 3, 20)  # a Friday
DAY_2 = date(2026, 3, 23)  # the Monday after: three calendar days


@pytest.fixture
def env(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    reports = [SurvivalReport(test_id="oos", passed=True, metrics={})]
    registry.register(
        BuyAndHold({"ticker": "UP.US", "allocation": 0.4}), reports=reports, strategy_id="bh_up"
    )
    seed_status(registry, "bh_up", "active")
    yield lake_trending, state, registry
    state.close()


def _short_book(state: SqliteState, *, allow_short: bool = True) -> str:
    people = People(state)
    pid = people.book(
        people.trader("Sam"),
        "Shorts",
        {"bh_up": 1.0},
        initial_cash=10_000.0,
        allow_short=allow_short,
    )
    # A short of 10 DOWN.US carried into the first tick (shorts only come
    # from the snapshot until strategies can emit them, roadmap 16.3).
    state.execute(
        "INSERT INTO tick_runs (id, started_at, finished_at, status)"
        " VALUES ('seed', '2026-03-19T21:00:00+00:00', '2026-03-19T21:00:00+00:00', 'ok')"
    )
    state.execute(
        "INSERT INTO portfolio_snapshots (tick_id, portfolio_id, as_of, taken_at, cash,"
        " positions_json, total_value, source) VALUES (?, ?, ?, ?, ?, ?, ?, 'tick')",
        [
            "seed",
            pid,
            "2026-03-19",
            "2026-03-19T21:00:00+00:00",
            10_700.0,
            json.dumps({"DOWN.US": -10.0}),
            10_000.0,
        ],
    )
    return pid


def _charges(state: SqliteState, pid: str) -> list[dict]:
    return state.sql(
        "SELECT as_of, ticker, kind, amount, days FROM financing_charges"
        " WHERE portfolio_id = ? ORDER BY id",
        [pid],
    )


def _tick(env, day: date, dry_run: bool = False):
    lake, state, registry = env
    plan = load_tick_plan(state, SETTINGS)
    return run_tick(state, lake, registry, SETTINGS, as_of=day, plan=plan, dry_run=dry_run)


def _cash(state: SqliteState, pid: str) -> float:
    return state.sql(
        "SELECT cash FROM portfolio_snapshots WHERE portfolio_id = ? ORDER BY id DESC LIMIT 1",
        [pid],
    )[0]["cash"]


def _fee(days: int, price: float, qty: float = 10.0, rate: float = 0.005) -> float:
    # FlatBorrow's default equity fee: |qty| x price x rate / 360 per day
    return qty * price * rate * days / 360.0


def _close(lake, day: date) -> float:
    return float(
        lake.con.execute(
            "SELECT close FROM prices WHERE ticker = 'DOWN.US' AND date = ?", [day]
        ).fetchone()[0]
    )


def test_the_first_tick_charges_the_days_since_the_last_snapshot(env):
    lake, state, _ = env
    pid = _short_book(state)
    result = _tick(env, DAY_1)
    assert last_accrual(state, pid) == DAY_1
    charges = _charges(state, pid)
    assert [(c["as_of"], c["ticker"], c["kind"], c["days"]) for c in charges] == [
        ("2026-03-20", "DOWN.US", "borrow_fee", 1)
    ]
    assert charges[0]["amount"] == pytest.approx(-_fee(1, _close(lake, DAY_1)))
    book = next(b for b in result.portfolios if b.portfolio_id == pid)
    assert book.summary["financing"] == pytest.approx(charges[0]["amount"], abs=1e-6)


def test_the_next_tick_charges_from_the_stored_date_not_a_fresh_broker(env):
    lake, state, _ = env
    pid = _short_book(state)
    _tick(env, DAY_1)
    before = _cash(state, pid)
    _tick(env, DAY_2)
    assert last_accrual(state, pid) == DAY_2
    latest = _charges(state, pid)[-1]
    assert (latest["as_of"], latest["ticker"], latest["days"]) == ("2026-03-23", "DOWN.US", 3)
    assert latest["amount"] == pytest.approx(-_fee(3, _close(lake, DAY_2)))
    # the charge left the book's cash (buy and hold of UP.US trades nothing new)
    assert _cash(state, pid) == pytest.approx(before + latest["amount"])


def test_a_dry_run_neither_charges_nor_moves_the_clock(env):
    _, state, _ = env
    pid = _short_book(state)
    _tick(env, DAY_1)
    _tick(env, DAY_2, dry_run=True)
    assert last_accrual(state, pid) == DAY_1
    assert len(_charges(state, pid)) == 1


def test_a_long_only_book_keeps_no_accrual_clock(env):
    _, state, _ = env
    pid = _short_book(state, allow_short=False)
    _tick(env, DAY_1)
    assert last_accrual(state, pid) is None
    assert _charges(state, pid) == []
