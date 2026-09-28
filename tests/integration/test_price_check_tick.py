"""The tick holds the opening orders of tickers the day's second-source price
check gapped (roadmap 23.6). Closes still go out (P28)."""

from __future__ import annotations

import json
from datetime import date

import pandas as pd

from stonks.production.tick import TickSettings, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status

AS_OF = date(2026, 3, 20)


def _seed(lake):
    days = pd.bdate_range("2026-01-02", AS_OF)
    lake.upsert_prices(
        pd.DataFrame(
            [
                {"ticker": "UP.US", "date": d.date(), "open": 10.0, "high": 10.0, "low": 10.0,
                 "close": 10.0, "adj_close": 10.0, "volume": 1_000_000}
                for d in days
            ]
        )
    )  # fmt: skip
    lake.con.execute("INSERT INTO instruments (id, asset_class) VALUES ('UP.US', 'equity')")


def _hold(state, tickers):
    state.execute(
        "INSERT INTO price_checks (as_of, checked_at, source, status, held_json)"
        " VALUES (?, '2026-03-20T20:40:00+00:00', 'yahoo', 'gaps', ?)",
        [AS_OF.isoformat(), json.dumps(tickers)],
    )


def _tick(state, lake, registry, day=AS_OF):
    return run_tick(
        state=state,
        lake=lake,
        registry=registry,
        settings=TickSettings(universe=["UP.US"], initial_cash=10_000.0),
        as_of=day,
    )


def test_a_gapped_ticker_is_not_bought(state, lake, tmp_path):
    _seed(lake)
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    sid = registry.register(BuyAndHold({"ticker": "UP.US", "allocation": 0.5}), reports=[])
    seed_status(registry, sid, "active")
    _hold(state, ["UP.US"])
    held = _tick(state, lake, registry)
    assert held.orders_placed == 0
    rows = state.sql("SELECT COUNT(*) AS n FROM orders WHERE ticker = 'UP.US'")
    assert rows[0]["n"] == 0
    # the next day has no hold: the buy goes out
    free = _tick(state, lake, registry, date(2026, 3, 23))
    assert free.orders_placed == 1
