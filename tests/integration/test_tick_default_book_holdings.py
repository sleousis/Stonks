"""The default portfolio keeps a book while it holds positions, even with no
strategy subscribed, so corporate actions on its holdings still apply on a
day nothing trades (18.7 follow-up to the per-portfolio books)."""

from __future__ import annotations

import json
from datetime import date

from stonks.accounts import DEFAULT_PORTFOLIO_ID
from stonks.production.tick import TickSettings, load_tick_plan, run_tick
from tests.integration.test_production_tick_corporate_actions import (  # noqa: F401 - fixture
    UNIVERSE,
    env,
)

SETTINGS = TickSettings(universe=UNIVERSE, initial_cash=10_000.0)


def _snapshot(state, as_of: str, positions: dict, cash: float = 0.0) -> None:
    state.execute(
        "INSERT OR IGNORE INTO tick_runs (id, started_at, status) VALUES ('t0', 'x', 'ok')"
    )
    state.execute(
        "INSERT INTO portfolio_snapshots (tick_id, portfolio_id, as_of, taken_at, cash,"
        " positions_json, total_value) VALUES ('t0', ?, ?, 'x', ?, ?, 0)",
        [DEFAULT_PORTFOLIO_ID, as_of, cash, json.dumps(positions)],
    )


def _retire_everything(state) -> None:
    state.execute("DELETE FROM subscriptions")


def test_an_empty_default_portfolio_gets_no_book(env):  # noqa: F811 - the imported fixture
    _, state, _ = env
    _retire_everything(state)
    assert load_tick_plan(state, SETTINGS).books == ()


def test_a_default_portfolio_with_holdings_keeps_its_book(env):  # noqa: F811 - the imported fixture
    _, state, _ = env
    _retire_everything(state)
    _snapshot(state, "2026-03-17", {"FLAT.US": 10.0})
    books = load_tick_plan(state, SETTINGS).books
    assert [b.portfolio_id for b in books] == [DEFAULT_PORTFOLIO_ID]
    assert books[0].mode == "paper" and not books[0].spec.strategy_weights


def test_corporate_actions_apply_with_no_strategy_subscribed(env):  # noqa: F811 - the imported fixture
    lake, state, registry = env
    _retire_everything(state)
    _snapshot(state, "2026-03-17", {"FLAT.US": 10.0}, cash=100.0)
    plan = load_tick_plan(state, SETTINGS)
    result = run_tick(state, lake, registry, SETTINGS, as_of=date(2026, 3, 20), plan=plan)

    assert result.orders_placed == 0
    row = state.sql(
        "SELECT cash, positions_json FROM portfolio_snapshots WHERE portfolio_id = ?"
        " ORDER BY as_of DESC, id DESC LIMIT 1",
        [DEFAULT_PORTFOLIO_ID],
    )[0]
    # the 2:1 split doubled the shares, then 0.5 a share was paid on 20 shares
    assert json.loads(row["positions_json"]) == {"FLAT.US": 20.0}
    assert row["cash"] == 110.0
