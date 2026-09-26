"""The production tick applies splits and dividends to the stored real and
shadow portfolios before deciding, exactly once per event, and never an
event after ``as_of``."""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from stonks.core.protocols import SurvivalReport
from stonks.core.types import Order
from stonks.production.tick import TickSettings, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold

SPLIT_EX = date(2026, 3, 18)  # FLAT.US 2:1, raw close 50 -> 25
DIVIDEND_EX = date(2026, 3, 19)  # FLAT.US 0.5 per (post-split) share
UNIVERSE = ["UP.US", "FLAT.US", "DOWN.US"]
SETTINGS = TickSettings(universe=UNIVERSE, initial_cash=10_000.0)


class Holder(BuyAndHold):
    """Ranks nothing, never trades. Module scope for registry re-import."""

    def estimate_return(self, ticker, as_of, lake):
        return None

    def decide(self, my_picks, portfolio, prices, as_of):
        return []


class SellEverything(Holder):
    """Sells every holding it is shown, at the quantity it is shown."""

    def decide(self, my_picks, portfolio, prices, as_of):
        return [
            Order(client_id=f"s:{t}", ticker=t, side="sell", quantity=q)
            for t, q in portfolio.positions.items()
            if q > 0
        ]


@pytest.fixture
def env(tmp_path, lake_trending):
    lake = lake_trending
    after = pd.bdate_range(SPLIT_EX, "2026-04-01")
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": "FLAT.US",
                "date": [d.date() for d in after],
                "open": 25.0,
                "high": 25.0,
                "low": 25.0,
                "close": 25.0,
                "adj_close": 25.0,
                "volume": 1_000_000,
            }
        )
    )
    lake.upsert_stock_splits(pd.DataFrame([{"ticker": "FLAT.US", "date": SPLIT_EX, "ratio": 2.0}]))
    lake.upsert_dividends(
        pd.DataFrame(
            [
                {
                    "ticker": "FLAT.US",
                    "ex_date": DIVIDEND_EX,
                    "amount": 0.5,
                    "currency": "USD",
                    "pay_date": None,
                    "record_date": None,
                    "declaration_date": None,
                }
            ]
        )
    )
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    yield lake, state, registry
    state.close()


def _register(registry, cls, status="active"):
    sid = registry.register(
        cls({"ticker": "UP.US", "allocation": 1.0}),
        reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
    )
    registry.set_status(sid, status)
    return sid


def _hold(state, strategy_id, positions, cash=0.0, as_of="2026-03-17"):
    state.execute(
        "INSERT OR IGNORE INTO tick_runs (id, started_at, status) VALUES ('t0', 'x', 'ok')"
    )
    for ticker, qty in positions.items():
        state.execute(
            "INSERT INTO orders (client_id, tick_id, strategy_id, ticker, side, quantity,"
            " order_type, status, created_at, updated_at)"
            " VALUES (?, 't0', ?, ?, 'buy', ?, 'market', 'filled', ?, ?)",
            [f"{as_of}:{strategy_id}:{ticker}:buy", strategy_id, ticker, qty, as_of, as_of],
        )
    state.execute(
        "INSERT INTO portfolio_snapshots (tick_id, as_of, taken_at, cash, positions_json,"
        " total_value) VALUES ('t0', ?, 'x', ?, ?, 0)",
        [as_of, cash, json.dumps(positions)],
    )


def _latest(state):
    row = state.sql("SELECT * FROM portfolio_snapshots ORDER BY as_of DESC, id DESC LIMIT 1")[0]
    return json.loads(row["positions_json"]), row["cash"], row["as_of"]


def _tick(env, as_of, settings=SETTINGS, **kw):
    lake, state, registry = env
    return run_tick(state, lake, registry, settings, as_of=as_of, **kw)


def test_decide_sees_split_adjusted_holdings_and_sizes_on_raw_quotes(env):
    lake, state, registry = env
    sid = _register(registry, SellEverything)
    _hold(state, sid, {"FLAT.US": 10.0})

    result = _tick(env, date(2026, 3, 20))

    assert result.fills == 1
    [sell] = state.sql("SELECT quantity FROM orders WHERE side = 'sell'")
    assert sell["quantity"] == pytest.approx(20.0)
    positions, cash, _ = _latest(state)
    assert positions == {}
    assert cash == pytest.approx(20 * 0.5 + 20 * 25.0)  # dividend + sale at raw 25


def test_each_event_is_applied_once_across_reruns_and_later_ticks(env):
    _, state, registry = env
    sid = _register(registry, Holder)
    _hold(state, sid, {"FLAT.US": 10.0})

    for as_of in (date(2026, 3, 20), date(2026, 3, 20), date(2026, 3, 23)):
        _tick(env, as_of)
        positions, cash, _ = _latest(state)
        assert positions == {"FLAT.US": 20.0}
        assert cash == pytest.approx(10.0)


def test_events_after_as_of_are_not_applied_until_their_ex_date(env):
    _, state, registry = env
    sid = _register(registry, Holder)
    _hold(state, sid, {"FLAT.US": 10.0}, as_of="2026-03-16")

    _tick(env, date(2026, 3, 17))
    assert _latest(state)[:2] == ({"FLAT.US": 10.0}, 0.0)
    _tick(env, SPLIT_EX)
    assert _latest(state)[:2] == ({"FLAT.US": 20.0}, 0.0)
    _tick(env, DIVIDEND_EX)
    assert _latest(state)[:2] == ({"FLAT.US": 20.0}, pytest.approx(10.0))


def test_events_on_or_before_the_snapshot_date_are_already_reflected(env):
    _, state, registry = env
    sid = _register(registry, Holder)
    # bought on the split's ex-date at the post-split price
    _hold(state, sid, {"FLAT.US": 20.0}, as_of=SPLIT_EX.isoformat())

    _tick(env, date(2026, 3, 20))
    positions, cash, _ = _latest(state)
    assert positions == {"FLAT.US": 20.0}
    assert cash == pytest.approx(10.0)  # the dividend only


def test_dividend_withholding(env):
    _, state, registry = env
    sid = _register(registry, Holder)
    _hold(state, sid, {"FLAT.US": 10.0})
    settings = TickSettings(universe=UNIVERSE, initial_cash=10_000.0, dividend_withholding_rate=0.3)

    _tick(env, date(2026, 3, 20), settings=settings)
    assert _latest(state)[1] == pytest.approx(20 * 0.5 * 0.7)


def test_withholding_rate_is_validated():
    with pytest.raises(ValueError, match="withholding"):
        TickSettings(universe=UNIVERSE, dividend_withholding_rate=1.5)


def test_noop_tick_persists_applied_events_once(env):
    _, state, registry = env
    sid = _register(registry, Holder)
    _hold(state, sid, {"FLAT.US": 10.0})
    registry.set_status(sid, "retired")  # no active owner: noop

    for _ in range(2):
        result = _tick(env, date(2026, 3, 20))
        assert result.status == "noop"
        positions, cash, as_of = _latest(state)
        assert (positions, as_of) == ({"FLAT.US": 20.0}, "2026-03-20")
        assert cash == pytest.approx(10.0)


def test_dry_run_persists_nothing_and_the_next_tick_applies_once(env):
    _, state, registry = env
    sid = _register(registry, Holder)
    _hold(state, sid, {"FLAT.US": 10.0})

    _tick(env, date(2026, 3, 20), dry_run=True)
    assert _latest(state)[:2] == ({"FLAT.US": 10.0}, 0.0)
    _tick(env, date(2026, 3, 20))
    assert _latest(state)[:2] == ({"FLAT.US": 20.0}, pytest.approx(10.0))


def test_tick_summary_lists_applied_events(env):
    _, state, registry = env
    sid = _register(registry, Holder)
    _hold(state, sid, {"FLAT.US": 10.0})

    result = _tick(env, date(2026, 3, 20))
    row = state.sql("SELECT summary_json FROM tick_runs WHERE id = ?", [result.tick_id])[0]
    applied = json.loads(row["summary_json"])["corporate_actions"]
    assert [(a["kind"], a["ticker"], a["ex_date"]) for a in applied] == [
        ("split", "FLAT.US", "2026-03-18"),
        ("dividend", "FLAT.US", "2026-03-19"),
    ]


def test_working_orders_from_earlier_ticks_are_split_adjusted_once(env):
    _, state, registry = env
    sid = _register(registry, Holder)
    _hold(state, sid, {"FLAT.US": 10.0})
    state.execute(
        "INSERT INTO orders (client_id, tick_id, strategy_id, ticker, side, quantity,"
        " order_type, limit_price, status, created_at, updated_at)"
        " VALUES ('open-1', 't0', ?, 'FLAT.US', 'buy', 4, 'limit', 50, 'pending', 'x', 'x')",
        [sid],
    )

    for _ in range(2):
        _tick(env, date(2026, 3, 20))
        [row] = state.sql("SELECT quantity, limit_price FROM orders WHERE client_id = 'open-1'")
        assert (row["quantity"], row["limit_price"]) == (8.0, 25.0)


def test_shadow_portfolio_gets_the_events_once(env):
    _, state, registry = env
    shadow = _register(registry, Holder, status="shadow")
    state.execute("INSERT INTO tick_runs (id, started_at, status) VALUES ('t0', 'x', 'ok')")
    state.execute(
        "INSERT INTO shadow_portfolio_snapshots"
        " (tick_id, strategy_id, as_of, taken_at, cash, positions_json, total_value)"
        " VALUES ('t0', ?, '2026-03-17', 'x', 0, ?, 0)",
        [shadow, json.dumps({"FLAT.US": 10.0})],
    )

    for as_of in (date(2026, 3, 20), date(2026, 3, 20), date(2026, 3, 23)):
        _tick(env, as_of)
        row = state.sql(
            "SELECT cash, positions_json FROM shadow_portfolio_snapshots"
            " WHERE strategy_id = ? ORDER BY as_of DESC, id DESC LIMIT 1",
            [shadow],
        )[0]
        assert json.loads(row["positions_json"]) == {"FLAT.US": 20.0}
        assert row["cash"] == pytest.approx(10.0)
