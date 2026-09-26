"""Exits without picks, and pricing of held tickers outside the universe."""

from __future__ import annotations

import json
from datetime import date

import pytest

from stonks.core.protocols import SurvivalReport
from stonks.core.types import Order
from stonks.production.tick import TickSettings, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold

AS_OF = date(2026, 3, 20)


class ExitWhenUnpicked(BuyAndHold):
    """Never ranks anything; sells every holding that isn't picked, and buys
    its target when picked. Module scope so the registry can re-import it."""

    def estimate_return(self, ticker, as_of, lake):
        return None

    def decide(self, my_picks, portfolio, prices, as_of):
        picked = {t for _, t in my_picks}
        orders = [
            Order(client_id=f"x:{t}", ticker=t, side="sell", quantity=q)
            for t, q in portfolio.positions.items()
            if q > 0 and t not in picked
        ]
        target = self.params["ticker"]
        if target in picked and prices.get(target):
            orders.append(Order(client_id="b", ticker=target, side="buy", quantity=1.0))
        return orders


class AlwaysBuyTarget(BuyAndHold):
    """Ignores picks: sells nothing, buys one share of its target."""

    def estimate_return(self, ticker, as_of, lake):
        return None

    def decide(self, my_picks, portfolio, prices, as_of):
        return [Order(client_id="b", ticker=self.params["ticker"], side="buy", quantity=1.0)]


@pytest.fixture
def env(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    yield lake_trending, state, registry
    state.close()


def _register(registry, strategy, status="active"):
    sid = registry.register(
        strategy, reports=[SurvivalReport(test_id="oos", passed=True, metrics={})]
    )
    registry.set_status(sid, status)
    return sid


def _hold(state, strategy_id, positions, cash=0.0, as_of="2026-03-19"):
    """Seed a prior tick in which ``strategy_id`` bought ``positions``."""
    state.execute("INSERT INTO tick_runs (id, started_at, status) VALUES ('t0', 'x', 'ok')")
    for ticker, qty in positions.items():
        state.execute(
            "INSERT INTO orders (client_id, tick_id, strategy_id, ticker, side, quantity,"
            " order_type, status, created_at, updated_at)"
            " VALUES (?, 't0', ?, ?, 'buy', ?, 'market', 'filled', ?, ?)",
            [f"{as_of}:{strategy_id}:{ticker}:buy", strategy_id, ticker, qty, as_of, as_of],
        )
    state.execute(
        "INSERT INTO portfolio_snapshots (tick_id, as_of, taken_at, cash, positions_json,"
        " total_value) VALUES ('t0', ?, ?, ?, ?, 0)",
        [as_of, f"{as_of}T22:45:00+00:00", cash, json.dumps(positions)],
    )


def _latest_snapshot(state):
    return state.sql("SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT 1")[0]


# ---- fix 1: exits without picks ---------------------------------------------


def test_owner_exits_positions_when_nothing_is_ranked(env):
    lake, state, registry = env
    sid = _register(registry, ExitWhenUnpicked({"ticker": "UP.US", "allocation": 1.0}))
    _hold(state, sid, {"DOWN.US": 10.0})

    settings = TickSettings(universe=["UP.US", "DOWN.US"], initial_cash=10_000.0)
    result = run_tick(state, lake, registry, settings, as_of=AS_OF)

    assert result.status == "ok"
    assert result.fills == 1
    sells = state.sql("SELECT strategy_id, ticker, status FROM orders WHERE side = 'sell'")
    assert [(r["strategy_id"], r["ticker"], r["status"]) for r in sells] == [
        (sid, "DOWN.US", "filled")
    ]
    snap = _latest_snapshot(state)
    assert snap["tick_id"] == result.tick_id
    assert json.loads(snap["positions_json"]) == {}
    assert snap["cash"] > 0


def test_no_exit_when_position_owner_is_not_active(env):
    lake, state, registry = env
    sid = _register(registry, ExitWhenUnpicked({"ticker": "UP.US", "allocation": 1.0}))
    _hold(state, sid, {"DOWN.US": 10.0})
    registry.set_status(sid, "retired")

    settings = TickSettings(universe=["UP.US", "DOWN.US"], initial_cash=10_000.0)
    result = run_tick(state, lake, registry, settings, as_of=AS_OF)

    assert result.status == "noop"
    assert state.count_rows("orders") == 1  # only the seeded buy


def test_flat_portfolio_without_picks_stays_noop(env):
    lake, state, registry = env
    _register(registry, ExitWhenUnpicked({"ticker": "UP.US", "allocation": 1.0}))
    settings = TickSettings(universe=["UP.US"], initial_cash=10_000.0)
    result = run_tick(state, lake, registry, settings, as_of=AS_OF)
    assert result.status == "noop"
    assert state.count_rows("portfolio_snapshots") == 0


def test_shadow_strategy_with_positions_exits_without_picks(env):
    lake, state, registry = env
    sid = _register(registry, ExitWhenUnpicked({"ticker": "UP.US", "allocation": 1.0}), "shadow")
    state.execute("INSERT INTO tick_runs (id, started_at, status) VALUES ('t0', 'x', 'ok')")
    state.execute(
        "INSERT INTO shadow_portfolio_snapshots (tick_id, strategy_id, as_of, taken_at, cash,"
        " positions_json, total_value) VALUES ('t0', ?, '2026-03-19', 'x', 0, ?, 0)",
        [sid, json.dumps({"DOWN.US": 10.0})],
    )
    settings = TickSettings(universe=["UP.US", "DOWN.US"], initial_cash=10_000.0)
    run_tick(state, lake, registry, settings, as_of=AS_OF)

    decisions = state.sql("SELECT ticker, side, status FROM shadow_decisions")
    assert [(d["ticker"], d["side"], d["status"]) for d in decisions] == [
        ("DOWN.US", "sell", "filled")
    ]
    snap = state.sql(
        "SELECT positions_json FROM shadow_portfolio_snapshots WHERE as_of = ?",
        [AS_OF.isoformat()],
    )[0]
    assert json.loads(snap["positions_json"]) == {}


# ---- fix 2: prices for held tickers outside the universe ---------------------


def test_held_ticker_outside_universe_is_priced_and_sellable(env):
    lake, state, registry = env
    sid = _register(registry, ExitWhenUnpicked({"ticker": "UP.US", "allocation": 1.0}))
    _hold(state, sid, {"DOWN.US": 10.0})

    settings = TickSettings(universe=["UP.US"], initial_cash=10_000.0)
    run_tick(state, lake, registry, settings, as_of=AS_OF)

    sells = state.sql("SELECT ticker, status FROM orders WHERE side = 'sell'")
    assert [(r["ticker"], r["status"]) for r in sells] == [("DOWN.US", "filled")]


def test_held_ticker_outside_universe_is_marked_in_the_snapshot(env):
    lake, state, registry = env
    sid = _register(registry, AlwaysBuyTarget({"ticker": "UP.US", "allocation": 1.0}))
    _hold(state, sid, {"DOWN.US": 10.0}, cash=1_000.0)

    settings = TickSettings(universe=["UP.US"], initial_cash=10_000.0)
    run_tick(state, lake, registry, settings, as_of=AS_OF)

    snap = _latest_snapshot(state)
    positions = json.loads(snap["positions_json"])
    assert positions["DOWN.US"] == pytest.approx(10.0)
    down_close = lake.sql(
        "SELECT close FROM prices WHERE ticker = 'DOWN.US' AND date <= ? "
        "ORDER BY date DESC LIMIT 1",
        [AS_OF],
    ).iloc[0, 0]
    up_close = lake.sql(
        "SELECT close FROM prices WHERE ticker = 'UP.US' AND date <= ? ORDER BY date DESC LIMIT 1",
        [AS_OF],
    ).iloc[0, 0]
    expected = snap["cash"] + 10.0 * down_close + positions.get("UP.US", 0.0) * up_close
    assert snap["total_value"] == pytest.approx(expected)


def test_stale_close_blocks_buys_but_still_marks_and_sells(env):
    lake, state, registry = env
    # lake_trending's last bar is 2026-04-01; 2026-04-20 is past the 7-day window
    stale_day = date(2026, 4, 20)
    sid = _register(registry, ExitWhenUnpicked({"ticker": "UP.US", "allocation": 1.0}))
    _hold(state, sid, {"DOWN.US": 10.0, "FLAT.US": 5.0}, as_of="2026-04-01")
    settings = TickSettings(universe=["UP.US"], initial_cash=10_000.0)
    run_tick(state, lake, registry, settings, as_of=stale_day)

    sells = state.sql("SELECT ticker, status FROM orders WHERE side = 'sell' ORDER BY ticker")
    assert [(r["ticker"], r["status"]) for r in sells] == [
        ("DOWN.US", "filled"),
        ("FLAT.US", "filled"),
    ]
    snap = _latest_snapshot(state)
    # 10 * 60 (DOWN's last close) + 5 * 50 (FLAT's), no fees/slippage
    assert snap["total_value"] == pytest.approx(10 * 60.0 + 5 * 50.0)


def test_stale_close_blocks_new_buys(env):
    lake, state, registry = env
    sid = _register(registry, AlwaysBuyTarget({"ticker": "UP.US", "allocation": 1.0}))
    _hold(state, sid, {"UP.US": 1.0}, cash=5_000.0, as_of="2026-04-01")
    settings = TickSettings(universe=["UP.US"], initial_cash=10_000.0)
    result = run_tick(state, lake, registry, settings, as_of=date(2026, 4, 20))

    assert state.count_rows("orders") == 1  # only the seeded buy
    assert result.fills == 0
    snap = _latest_snapshot(state)
    assert snap["total_value"] == pytest.approx(5_000.0 + 200.0)  # UP's last close is 200
