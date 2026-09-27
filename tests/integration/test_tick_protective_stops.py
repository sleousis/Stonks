"""Protective stops in the tick (roadmap 19.10).

Off by default. When a book turns them on, every position it owns gets a
good till cancelled stop after the entry fills: at the broker for a live
book (the fake trading connection here), and in the ledger for a simulated
book, where the simulated broker fills it from the bar range of a later
day. A stop is cancelled when the position closes or stops turn off, an
exit is never dropped because a stop works on the same ticker, and a stop
fill counts as the strategy's stop-out."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pandas as pd
import pytest

import tests.integration.test_tick_modes as modes
from stonks.core.types import Order
from stonks.logging import get_logger
from stonks.production.live.stops import load_working_stops, tag_exits
from stonks.production.live.trades import closed_trades
from stonks.production.risk import RiskPolicy
from stonks.production.rules.settings import RuleSettings
from stonks.production.tick import _drop_open_order_conflicts, load_tick_plan, run_tick

DAY1, DAY2 = modes.DAY1, modes.DAY2
STOPS_ON = RiskPolicy(rules=RuleSettings.model_validate({"protective_stops": {"enabled": True}}))
SETTINGS_ON = replace(modes.SETTINGS, risk=STOPS_ON)


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


def tick(world, as_of, settings=SETTINGS_ON):
    plan = load_tick_plan(world.state, settings, traders=world.traders)
    return run_tick(world.state, world.lake, world.registry, settings, as_of=as_of, plan=plan)


def stops(world, portfolio_id):
    return world.state.sql(
        "SELECT client_id, ticker, side, quantity, stop_price, state, status, status_reason,"
        " strategy_id, oca_group FROM orders WHERE portfolio_id = ? AND protective = 1"
        " ORDER BY created_at, rowid",
        [portfolio_id],
    )


def position(world, portfolio_id, ticker):
    [snap] = world.state.sql(
        "SELECT positions_json FROM portfolio_snapshots WHERE portfolio_id = ?"
        " ORDER BY rowid DESC LIMIT 1",
        [portfolio_id],
    )
    return json.loads(snap["positions_json"]).get(ticker, 0.0)


def summary(result, portfolio_id):
    return next(r for r in result.portfolios if r.portfolio_id == portfolio_id).summary


def test_off_by_default(world):
    tick(world, DAY1, settings=modes.SETTINGS)
    assert stops(world, world.sim) == [] and stops(world, world.live) == []
    assert world.book.resting == {}


def test_a_simulated_book_gets_a_working_stop_after_its_entry(world):
    result = tick(world, DAY1)
    held = position(world, world.sim, "UP.US")
    assert held > 0
    [stop] = stops(world, world.sim)
    entry = f"2026-03-17:{world.sim}:bh_up:UP.US:buy"
    assert stop["client_id"] == f"{entry}:stop"
    assert (stop["side"], stop["quantity"], stop["state"]) == ("sell", pytest.approx(held),
                                                               "accepted")  # fmt: skip
    assert stop["strategy_id"] == "bh_up"
    [close] = world.lake.sql("SELECT close FROM prices WHERE ticker = 'UP.US' AND date = ?",
                             [DAY1])["close"]  # fmt: skip
    assert close - 10 < stop["stop_price"] < close
    assert summary(result, world.sim)["stops"]["placed"] == 1

    tick(world, DAY1)  # a same-day re-run places nothing more
    assert len(stops(world, world.sim)) == 1


def test_a_live_book_sends_its_stop_to_the_broker(world):
    tick(world, DAY1)
    [stop] = stops(world, world.live)
    assert stop["client_id"] in world.book.resting
    assert (stop["side"], stop["state"], stop["ticker"]) == ("sell", "accepted", "UP.US")
    _, resting = world.book.resting[stop["client_id"]]
    assert (resting.order_type, resting.time_in_force) == ("stop", "gtc")
    assert resting.stop_price == pytest.approx(stop["stop_price"])
    # the broker portfolio's paper account is simulated: its stop stays in the ledger
    paper = modes.paper_account_id(world.live)
    assert [s["ticker"] for s in stops(world, paper)] == ["DOWN.US"]


def test_a_simulated_stop_fills_from_a_later_bar_and_counts_as_a_stop_out(world):
    tick(world, DAY1)
    [stop] = stops(world, world.sim)
    level = stop["stop_price"]
    crash = pd.DataFrame([{"ticker": "UP.US", "date": DAY2, "open": level + 2, "high": level + 3,
                           "low": level - 5, "close": level - 4, "adj_close": level - 4,
                           "volume": 1_000_000}])  # fmt: skip
    world.lake.upsert_prices(crash)
    tick(world, DAY2)
    rows = {r["client_id"]: r for r in stops(world, world.sim)}
    assert rows[stop["client_id"]]["state"] == "filled"
    [fill] = world.state.sql("SELECT quantity, price FROM fills WHERE order_client_id = ?",
                             [stop["client_id"]])  # fmt: skip
    assert fill["price"] == pytest.approx(stop["stop_price"])
    assert fill["quantity"] == pytest.approx(stop["quantity"])
    [trade] = [t for t in closed_trades(world.state, world.sim, DAY2) if t.stop]
    assert trade.strategy_id == "bh_up"


def test_turning_stops_off_cancels_the_working_ones(world):
    tick(world, DAY1)
    [live_stop] = stops(world, world.live)
    tick(world, DAY2, settings=modes.SETTINGS)
    [after] = stops(world, world.live)
    assert (after["state"], after["status_reason"]) == ("cancelled",
                                                        "protective stops were turned off")  # fmt: skip
    assert world.book.resting == {}
    assert world.book.orders[live_stop["client_id"]].state == "cancelled"
    assert load_working_stops(world.state, world.sim) == []


def test_a_paper_exit_closes_the_position_and_cancels_its_stop(world):
    tick(world, DAY1)
    [stop] = stops(world, world.sim)
    world.registry.set_status("bh_up", "retired", actor="t", reason="no edge left")
    tick(world, DAY2)  # BE-18: the retired strategy's holding is sold
    assert position(world, world.sim, "UP.US") == 0.0
    [after] = stops(world, world.sim)
    assert after["client_id"] == stop["client_id"]
    assert (after["state"], after["status_reason"]) == ("cancelled", "the position closed")


def test_an_exit_is_not_dropped_by_the_working_stop_and_joins_its_group(world):
    tick(world, DAY1)
    [stop] = stops(world, world.live)
    exit_order = Order(client_id="2026-03-18:x", ticker="UP.US", side="sell", quantity=1.0,
                       strategy_id="bh_up", position_effect="close")  # fmt: skip
    kept, conflicts = _drop_open_order_conflicts(
        world.state, [exit_order], get_logger("t"), portfolio_id=world.live
    )
    assert kept == [exit_order] and conflicts == []
    [tagged] = tag_exits(kept, load_working_stops(world.state, world.live))
    assert tagged.oca_group == stop["oca_group"]


def test_a_live_stop_fill_is_a_stop_out_and_the_re_entry_gets_a_fresh_stop(world):
    tick(world, DAY1)
    [stop] = stops(world, world.live)
    world.book.trigger_stop(stop["client_id"], stop["stop_price"])
    tick(world, DAY2)
    rows = {r["client_id"]: r for r in stops(world, world.live)}
    assert rows[stop["client_id"]]["state"] == "filled"
    # reconciliation books the fill at the real time it learns of it
    today = datetime.now(UTC).date()
    [trade] = [t for t in closed_trades(world.state, world.live, today) if t.stop]
    assert trade.strategy_id == "bh_up"
    # BuyAndHold buys again: the new entry has its own stop at the broker
    fresh = f"2026-03-18:{world.live}:bh_up:UP.US:buy:stop"
    assert rows[fresh]["state"] == "accepted" and fresh in world.book.resting


def test_a_portfolio_can_turn_stops_on_for_itself(world):
    policy = {"rules": {"protective_stops": {"enabled": True}}}
    world.state.execute(
        "UPDATE portfolios SET risk_policy_json = ? WHERE id = ?", [json.dumps(policy), world.sim]
    )
    tick(world, DAY1, settings=modes.SETTINGS)
    assert len(stops(world, world.sim)) == 1
    assert stops(world, world.live) == []


def test_a_dry_run_places_no_stop(world):
    plan = load_tick_plan(world.state, SETTINGS_ON, traders=world.traders)
    run_tick(world.state, world.lake, world.registry, SETTINGS_ON, as_of=DAY1, plan=plan,
             dry_run=True)  # fmt: skip
    assert stops(world, world.sim) == [] and world.book.resting == {}
