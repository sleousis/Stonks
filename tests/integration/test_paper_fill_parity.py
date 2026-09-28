"""Paper books fill like the backtest (P21).

The same strategy over the same days gives the same fills in the paper
tick loop and in a backtest: at the next session's open, through the same
fill model (participation cap, gap guard) and cost model."""

from __future__ import annotations

import json
import math
from datetime import date

import pandas as pd
import pytest

from stonks.backtest.costs import CostModelSettings
from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.fills import ExecutionSettings, FillModelSettings
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.protocols import SurvivalReport
from stonks.core.types import Portfolio
from stonks.ingest.pipeline import _rows_to_df
from stonks.ingest.schemas import TickerProfile
from stonks.production.risk import RiskPolicy
from stonks.production.tick import TickPlan, TickSettings, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from stonks.strategies.examples.momentum import Momentum
from tests.fixtures.governance import seed_status

TICKERS = ["A.US", "B.US"]
START, END = date(2026, 3, 2), date(2026, 4, 10)
#: The day B.US trades thin: the participation cap fills part of an order.
THIN = date(2026, 3, 9)
EXECUTION = ExecutionSettings(fill=FillModelSettings(max_participation=0.1))
RISK = RiskPolicy()


def _strategy() -> Momentum:
    return Momentum({"lookback_days": 5, "threshold": 0.0, "allocation": 0.9})


@pytest.fixture
def lake(tmp_path):
    """Two tickers that swap the lead every few weeks, opens away from the
    closes, and one thin day."""
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    dates = pd.bdate_range("2025-12-01", "2026-04-30")
    rows = []
    for ticker, phase in (("A.US", 0.0), ("B.US", 3.14159)):
        for i, d in enumerate(dates):
            close = 100.0 + 10.0 * math.sin(i / 6.0 + phase) + 0.1 * i
            open_ = close * (1.0 + 0.004 * math.cos(i + phase))
            volume = 300.0 if (ticker == "B.US" and d.date() == THIN) else 50_000.0 + 97 * i
            rows.append(
                {
                    "ticker": ticker,
                    "date": d.date(),
                    "open": open_,
                    "high": max(open_, close) + 0.7,
                    "low": min(open_, close) - 0.6,
                    "close": close,
                    "adj_close": close,
                    "volume": volume,
                }
            )
    lake.upsert_prices(pd.DataFrame(rows))
    lake.upsert_instrument_profile(
        _rows_to_df([TickerProfile(id=t, asset_class="equity") for t in TICKERS])
    )
    yield lake
    lake.close()


def _days(lake) -> list[date]:
    frame = lake.sql(
        "SELECT DISTINCT date FROM prices WHERE date BETWEEN ? AND ? ORDER BY date", [START, END]
    )
    return [pd.Timestamp(d).date() for d in frame["date"]]


def _backtest_fills(lake) -> list[tuple]:
    costs = CostModelSettings.realistic()
    broker = SimulatedBroker.from_execution(
        Portfolio(cash=10_000.0), EXECUTION, cost_model=costs.build()
    )
    Backtester(
        strategies=[_strategy()],
        broker=broker,
        lake=lake,
        config=BacktestConfig(
            start=START, end=END, universe=TICKERS, construction="single_winner", risk=RISK
        ),
    ).run()
    return [
        _row(f.filled_at.date(), f.ticker, f.side, f.quantity, f.price, f.fee) for f in broker.fills
    ]


def _row(day, ticker, side, quantity, price, fee) -> tuple:
    return (day, ticker, side, round(quantity, 6), round(price, 6), round(fee, 6))


def _tick_fills(lake, tmp_path) -> tuple[list[tuple], SqliteState]:
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    sid = registry.register(
        _strategy(), reports=[SurvivalReport(test_id="oos", passed=True, metrics={})]
    )
    seed_status(registry, sid, "active")
    settings = TickSettings(
        universe=TICKERS,
        initial_cash=10_000.0,
        costs=CostModelSettings.realistic(),
        risk=RISK,
        shadow_enabled=False,
        execution=EXECUTION,
        paper_fills="next_open",
    )
    for day in _days(lake):
        run_tick(state, lake, registry, settings, as_of=day, plan=TickPlan.default(settings))
    rows = state.sql(
        "SELECT f.filled_at, f.ticker, o.side, f.quantity, f.price, f.fee FROM fills f"
        " JOIN orders o ON o.client_id = f.order_client_id ORDER BY f.id"
    )
    fills = [
        _row(
            date.fromisoformat(r["filled_at"][:10]),
            r["ticker"],
            r["side"],
            r["quantity"],
            r["price"],
            r["fee"],
        )
        for r in rows
    ]
    return fills, state


def test_the_paper_tick_fills_like_the_backtest(lake, tmp_path):
    expected = _backtest_fills(lake)
    got, state = _tick_fills(lake, tmp_path)
    assert len(expected) >= 4, "the strategy must trade more than once"
    assert any(day == THIN for day, *_ in expected), "the thin day must cap a fill"
    assert got == expected
    # the thin day's order filled part and the next decision replaced the rest
    reasons = [
        r["status_reason"]
        for r in state.sql("SELECT status_reason FROM orders WHERE status = 'cancelled'")
    ]
    assert any("replaced by the next decision" in (r or "") for r in reasons)
    state.close()


def test_the_last_decision_is_still_working(lake, tmp_path):
    _, state = _tick_fills(lake, tmp_path)
    last = _days(lake)[-1].isoformat()
    working = state.sql("SELECT status, decided_at FROM orders WHERE status = 'pending'")
    assert all(r["decided_at"][:10] == last for r in working)
    snap = state.sql("SELECT positions_json FROM portfolio_snapshots ORDER BY id DESC LIMIT 1")[0]
    assert json.loads(snap["positions_json"])  # the book holds what filled
    state.close()


def test_tca_arrival_is_the_open_and_the_convention_gap_is_gone(lake, tmp_path):
    from stonks.production.tca import load_order_tca

    _, state = _tick_fills(lake, tmp_path)
    rows = [r for r in load_order_tca(state, None) if r.shortfall.filled_quantity > 0]
    assert rows
    for r in rows:
        assert r.shortfall.convention_bps == pytest.approx(0.0, abs=1e-9)
    state.close()


def test_a_model_book_fills_like_the_backtest(lake, tmp_path):
    """A shadow strategy's model book fills at the next open too."""
    expected = _backtest_fills(lake)
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    sid = registry.register(
        _strategy(), reports=[SurvivalReport(test_id="oos", passed=True, metrics={})]
    )
    seed_status(registry, sid, "shadow")
    settings = TickSettings(
        universe=TICKERS,
        initial_cash=10_000.0,
        costs=CostModelSettings.realistic(),
        risk=RISK,
        execution=EXECUTION,
        paper_fills="next_open",
    )
    for day in _days(lake):
        run_tick(state, lake, registry, settings, as_of=day, plan=TickPlan.default(settings))
    rows = state.sql(
        "SELECT filled_on, ticker, side, quantity, price FROM shadow_decisions"
        " WHERE status = 'filled' ORDER BY filled_on, id"
    )
    got = [
        (
            date.fromisoformat(r["filled_on"]),
            r["ticker"],
            r["side"],
            round(r["quantity"], 6),
            round(r["price"], 6),
        )
        for r in rows
    ]
    assert got == [row[:5] for row in expected]
    last = _days(lake)[-1].isoformat()
    working = state.sql("SELECT as_of FROM shadow_decisions WHERE status = 'working'")
    assert all(r["as_of"] == last for r in working)
    state.close()
