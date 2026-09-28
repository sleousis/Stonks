"""Parity (roadmap 21.2.5, docs/design/intraday.md section 7): one recorded
minute stream through the engine's replay path gives the same orders as
the intraday backtest over the same bars."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.accounts.models import DEFAULT_PORTFOLIO_ID
from stonks.backtest.fills import MinuteFillModel
from stonks.backtest.intraday import IntradayBacktestConfig, IntradayBacktester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Order, Portfolio
from stonks.engine.process import RoutedDecision
from stonks.engine.sessions import SessionRules
from tests.unit.engine.engine_fixtures import (
    PARITY_FILLS,
    book,
    parity_costs,
    record,
    replay_process,
)
from tests.unit.engine.minute_lake import DAYS, TICKERS, MinuteMomentum, minute_frame, minute_lake

PF = DEFAULT_PORTFOLIO_ID


@pytest.fixture(scope="module")
def lake():
    lk = minute_lake()
    yield lk
    lk.close()


def _key(order: Order, prefix: str = "") -> tuple:
    cid = order.client_id.removeprefix(prefix)
    return (
        cid,
        order.ticker,
        order.side,
        round(order.quantity, 9),
        order.order_type,
        order.strategy_id,
        order.position_effect,
    )


def backtest_orders(lake, day: date, sessions: SessionRules | None) -> list[list[tuple]]:
    broker = SimulatedBroker(
        Portfolio(cash=100_000.0),
        cost_model=parity_costs(),
        fill_model=MinuteFillModel(PARITY_FILLS),
    )
    config = IntradayBacktestConfig(start=day, end=day, universe=TICKERS, sessions=sessions)
    bt = IntradayBacktester([MinuteMomentum({"lookback": 10})], broker, lake, config)
    bt.run()
    return [[_key(o) for o in d.orders] for d in bt.decisions]


def replay_orders(lake, state, tmp_path, day: date, sessions: SessionRules | None):
    frame = minute_frame()
    import pandas as pd

    rec = record(frame[pd.to_datetime(frame["timestamp"]).dt.date == day], tmp_path / "rec")
    routed: list[RoutedDecision] = []
    process = replay_process(
        lake, state, rec, [book(PF, sessions=sessions)], session=day, decisions=routed
    )
    stats = process.run()
    assert process.driver.stats.total_handler_errors == 0
    return [[_key(o, f"{PF}:") for o in r.orders] for r in routed], routed, stats


@pytest.mark.parametrize(
    "sessions",
    [None, SessionRules(entry_delay_minutes=5, entry_cutoff_minutes=10, flatten_at_close=True)],
    ids=["no_session_rules", "flatten_at_close"],
)
def test_replay_path_matches_the_intraday_backtest(lake, state, tmp_path, sessions) -> None:
    day = DAYS[0]
    expected = backtest_orders(lake, day, sessions)
    got, routed, stats = replay_orders(lake, state, tmp_path, day, sessions)
    assert sum(len(d) for d in expected) > 20, "the toy strategy trades"
    assert len(got) == len(expected)
    assert got == expected
    # every order went to the broker once
    assert stats.orders_sent == sum(len(d) for d in expected)
    assert stats.orders_known == stats.orders_held == stats.orders_rejected == 0
    if sessions is not None:
        assert any(r.decision.flattened for r in routed)
        assert stats.flatten_orders > 0


def test_replay_books_the_same_fills_as_the_backtest(lake, state, tmp_path) -> None:
    day = DAYS[0]
    broker = SimulatedBroker(
        Portfolio(cash=100_000.0),
        cost_model=parity_costs(),
        fill_model=MinuteFillModel(PARITY_FILLS),
    )
    config = IntradayBacktestConfig(start=day, end=day, universe=TICKERS)
    IntradayBacktester([MinuteMomentum({"lookback": 10})], broker, lake, config).run()
    expected = sorted(
        (f.order_client_id, f.ticker, round(f.quantity, 9), round(f.price, 9)) for f in broker.fills
    )
    replay_orders(lake, state, tmp_path, day, None)
    rows = state.sql(
        "SELECT order_client_id, ticker, quantity, price FROM fills WHERE portfolio_id = ?", [PF]
    )
    got = sorted(
        (r["order_client_id"].removeprefix(f"{PF}:"), r["ticker"], round(r["quantity"], 9),
         round(r["price"], 9))
        for r in rows
    )  # fmt: skip
    assert got == expected
