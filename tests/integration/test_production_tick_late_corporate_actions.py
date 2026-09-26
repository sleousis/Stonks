"""TO-05: corporate actions are applied by ex-date, exactly once per
portfolio (the ``corporate_action_ledger``), even when the event row lands
in the lake after the ex-date tick, and never before the ex-date bar is in
the lake (the quote would still be in the pre-event basis)."""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from stonks.production.tick import TickSettings, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from tests.integration.test_production_tick_corporate_actions import (
    DIVIDEND_EX,
    SPLIT_EX,
    UNIVERSE,
    Holder,
    _register,
)

SETTINGS = TickSettings(universe=UNIVERSE, initial_cash=10_000.0)


def _bars(lake, ticker, start, end, close):
    days = pd.bdate_range(start, end)
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": ticker,
                "date": [d.date() for d in days],
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "adj_close": close,
                "volume": 1_000_000,
            }
        )
    )


def _split(lake, ticker, ex_date, ratio):
    lake.upsert_stock_splits(pd.DataFrame([{"ticker": ticker, "date": ex_date, "ratio": ratio}]))


def _dividend(lake, ticker, ex_date, amount):
    lake.upsert_dividends(
        pd.DataFrame(
            [
                {
                    "ticker": ticker,
                    "ex_date": ex_date,
                    "amount": amount,
                    "currency": "USD",
                    "pay_date": None,
                    "record_date": None,
                    "declaration_date": None,
                }
            ]
        )
    )


@pytest.fixture
def env(tmp_path, lake_trending):
    """FLAT.US quotes 25 from the split's ex-date on (post-split), but no
    split or dividend row is in the lake yet."""
    _bars(lake_trending, "FLAT.US", SPLIT_EX, "2026-04-01", 25.0)
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    sid = _register(registry, Holder)
    yield lake_trending, state, registry, sid
    state.close()


def _snapshot(state, as_of: str, positions: dict, cash: float = 0.0) -> None:
    state.execute(
        "INSERT OR IGNORE INTO tick_runs (id, started_at, status) VALUES ('t0', 'x', 'ok')"
    )
    state.execute(
        "INSERT INTO portfolio_snapshots (tick_id, as_of, taken_at, cash, positions_json,"
        " total_value) VALUES ('t0', ?, 'x', ?, ?, 0)",
        [as_of, cash, json.dumps(positions)],
    )


def _latest(state):
    row = state.sql("SELECT * FROM portfolio_snapshots ORDER BY as_of DESC, id DESC LIMIT 1")[0]
    return json.loads(row["positions_json"]), row["cash"]


def _tick(env, as_of, **kw):
    lake, state, registry, _ = env
    return run_tick(state, lake, registry, SETTINGS, as_of=as_of, **kw)


def _summary(state, tick_id):
    row = state.sql("SELECT summary_json FROM tick_runs WHERE id = ?", [tick_id])[0]
    return json.loads(row["summary_json"])


def test_a_split_that_arrives_after_the_ex_date_tick_is_applied_next_tick(env):
    lake, state, _, _ = env
    _snapshot(state, "2026-03-17", {"FLAT.US": 10.0})
    # the 03-19 tick marked the old quantity at the post-split quote: the
    # split row wasn't in the lake yet
    _snapshot(state, "2026-03-19", {"FLAT.US": 10.0})

    _split(lake, "FLAT.US", SPLIT_EX, 2.0)
    for as_of in (date(2026, 3, 20), date(2026, 3, 20), date(2026, 3, 23)):
        _tick(env, as_of)
        assert _latest(state)[0] == {"FLAT.US": 20.0}  # applied once, reruns included


def test_a_late_split_scales_only_the_shares_held_before_the_ex_date(env):
    """5 shares bought after the ex-date were bought at post-split prices."""
    lake, state, _, _ = env
    _snapshot(state, "2026-03-17", {"FLAT.US": 10.0})
    _snapshot(state, "2026-03-19", {"FLAT.US": 15.0})
    _split(lake, "FLAT.US", SPLIT_EX, 2.0)

    _tick(env, date(2026, 3, 20))

    assert _latest(state)[0] == {"FLAT.US": 25.0}


def test_a_late_split_skips_a_position_sold_and_bought_back_since(env):
    lake, state, _, _ = env
    _snapshot(state, "2026-03-17", {"FLAT.US": 10.0})
    _snapshot(state, "2026-03-18", {}, cash=250.0)
    _snapshot(state, "2026-03-19", {"FLAT.US": 4.0}, cash=150.0)
    _split(lake, "FLAT.US", SPLIT_EX, 2.0)

    _tick(env, date(2026, 3, 20))

    assert _latest(state)[0] == {"FLAT.US": 4.0}


def test_a_late_dividend_is_credited_on_the_ex_date_quantity(env):
    lake, state, _, _ = env
    _snapshot(state, "2026-03-17", {"FLAT.US": 10.0})
    _split(lake, "FLAT.US", SPLIT_EX, 2.0)
    _tick(env, date(2026, 3, 20))
    _dividend(lake, "FLAT.US", DIVIDEND_EX, 0.5)  # arrives late

    _tick(env, date(2026, 3, 23))

    positions, cash = _latest(state)
    assert positions == {"FLAT.US": 20.0}
    assert cash == pytest.approx(20 * 0.5)  # per post-split share held on the ex-date


def test_a_dividend_on_a_position_bought_on_the_ex_date_is_not_credited(env):
    lake, state, _, _ = env
    _snapshot(state, "2026-03-17", {}, cash=1_000.0)
    _snapshot(state, DIVIDEND_EX.isoformat(), {"FLAT.US": 10.0}, cash=750.0)
    _dividend(lake, "FLAT.US", DIVIDEND_EX, 0.5)

    _tick(env, date(2026, 3, 20))

    assert _latest(state)[1] == pytest.approx(750.0)


def test_a_split_without_its_ex_date_bar_is_deferred_not_applied(env):
    """DOWN.US's last bar is 2026-04-01: a split on 2026-04-06 must wait for
    the 04-06 bar, or the book would double at the pre-split quote."""
    lake, state, _, _ = env
    _snapshot(state, "2026-04-01", {"DOWN.US": 10.0})
    _split(lake, "DOWN.US", date(2026, 4, 6), 2.0)

    result = _tick(env, date(2026, 4, 6))

    assert _latest(state)[0] == {"DOWN.US": 10.0}
    [deferred] = _summary(state, result.tick_id)["deferred_corporate_actions"]
    assert (deferred["ticker"], deferred["kind"], deferred["ex_date"]) == (
        "DOWN.US",
        "split",
        "2026-04-06",
    )

    _bars(lake, "DOWN.US", "2026-04-06", "2026-04-06", 30.0)  # the post-split bar lands
    _tick(env, date(2026, 4, 6))  # rerun of the same day
    assert _latest(state)[0] == {"DOWN.US": 20.0}
    _tick(env, date(2026, 4, 7))
    assert _latest(state)[0] == {"DOWN.US": 20.0}


def test_a_reverse_split_on_a_fractional_holding(env):
    lake, state, _, _ = env
    _snapshot(state, "2026-03-17", {"UP.US": 3.3})
    _split(lake, "UP.US", SPLIT_EX, 0.1)

    _tick(env, date(2026, 3, 20))

    assert _latest(state)[0]["UP.US"] == pytest.approx(0.33)


def test_events_before_the_ledger_started_count_as_applied(env):
    """An upgraded install: events on or before its last snapshot at the
    upgrade were handled by the old rule and must not apply twice."""
    lake, state, _, _ = env
    _snapshot(state, "2026-03-17", {"FLAT.US": 10.0})
    state.execute(
        "INSERT INTO corporate_action_ledger_start (portfolio_id, start_as_of)"
        " VALUES ('pf_default', '2026-03-19')"
    )
    _split(lake, "FLAT.US", SPLIT_EX, 2.0)

    _tick(env, date(2026, 3, 20))

    assert _latest(state)[0] == {"FLAT.US": 10.0}


def test_a_working_order_placed_after_the_ex_date_is_not_rescaled(env):
    lake, state, _, sid = env
    _snapshot(state, "2026-03-17", {"FLAT.US": 10.0})
    for client_id, qty in (("2026-03-17:x:FLAT.US:buy", 4), ("2026-03-19:x:FLAT.US:buy", 6)):
        state.execute(
            "INSERT INTO orders (client_id, tick_id, strategy_id, ticker, side, quantity,"
            " order_type, limit_price, status, created_at, updated_at)"
            " VALUES (?, 't0', ?, 'FLAT.US', 'buy', ?, 'limit', 50, 'pending', 'x', 'x')",
            [client_id, sid, qty],
        )
    _split(lake, "FLAT.US", SPLIT_EX, 2.0)

    _tick(env, date(2026, 3, 20))

    rows = {
        r["client_id"]: r["quantity"]
        for r in state.sql("SELECT client_id, quantity FROM orders WHERE status = 'pending'")
    }
    assert rows == {"2026-03-17:x:FLAT.US:buy": 8.0, "2026-03-19:x:FLAT.US:buy": 6.0}
