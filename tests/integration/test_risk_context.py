"""``build_risk_context`` / ``load_history`` against a tmp lake and state (BL-11)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stonks.backtest.costs import CostModelSettings
from stonks.config import RiskPolicy
from stonks.core.types import Portfolio
from stonks.ingest.pipeline import _rows_to_df
from stonks.ingest.schemas import TickerProfile
from stonks.production.prices import load_history
from stonks.production.risk import build_risk_context
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

AS_OF = date(2026, 3, 31)


@pytest.fixture
def lake(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    dates = pd.bdate_range(end="2026-04-10", periods=320)
    rows = []
    for ticker, base in (("A.US", 10.0), ("B.US", 50.0)):
        for i, d in enumerate(dates):
            c = base + i * 0.1
            rows.append(
                {
                    "ticker": ticker,
                    "date": d.date(),
                    "open": c,
                    "high": c + 1,
                    "low": c - 1,
                    "close": c,
                    # A.US: a 2:1 split-style adjustment on every bar.
                    "adj_close": c / 2 if ticker == "A.US" else c,
                    "volume": 1_000 + i,
                }
            )
    lake.upsert_prices(pd.DataFrame(rows))
    lake.upsert_instrument_profile(
        _rows_to_df(
            [
                TickerProfile(id="A.US", name="A", asset_class="equity", sector="Technology"),
                TickerProfile(id="B.US", name="B", asset_class="equity"),
            ]
        )
    )
    yield lake
    lake.close()


@pytest.fixture
def state(tmp_path):
    with SqliteState(tmp_path / "state.sqlite") as st:
        st.migrate()
        yield st


def test_load_history_returns_last_bars_up_to_as_of_adjusted(lake):
    history = load_history(lake, ["A.US", "B.US", "MISSING.US"], AS_OF, bars=260)
    assert set(history) == {"A.US", "B.US"}
    a = history["A.US"]
    assert len(a) == 260
    assert a.index.max() <= pd.Timestamp(AS_OF)
    assert a.index.is_monotonic_increasing
    assert list(a.columns) == ["open", "high", "low", "close", "volume"]
    last = a.iloc[-1]
    raw_close = last["close"] * 2  # adjusted by adj_close / close = 0.5
    assert last["open"] == pytest.approx(raw_close / 2)
    assert last["high"] == pytest.approx((raw_close + 1) / 2)
    b = history["B.US"].iloc[-1]
    assert b["high"] - b["close"] == pytest.approx(1.0)


def test_load_history_uses_one_query(lake, monkeypatch):
    calls = []
    real = lake.sql

    def spy(query, params=None):
        calls.append(query)
        return real(query, params)

    monkeypatch.setattr(lake, "sql", spy)
    load_history(lake, ["A.US", "B.US"], AS_OF, bars=5)
    assert len(calls) == 1


def test_build_risk_context_loads_history_in_one_query(lake, state, monkeypatch):
    calls = []
    real = lake.sql

    def spy(query, params=None):
        calls.append(query)
        return real(query, params)

    monkeypatch.setattr(lake, "sql", spy)
    build_risk_context(lake, state, Portfolio(cash=0.0, positions={"A.US": 1.0}), {}, AS_OF)
    assert sum("adj_close" in q for q in calls) == 1


def _fill(state, client_id, ticker, side, qty, day):
    state.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, 'market', 'filled', ?, ?)",
        [client_id, ticker, side, qty, day, day],
    )
    state.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at)"
        " VALUES (?, ?, ?, 10.0, 0.0, ?)",
        [client_id, ticker, qty, f"{day}T20:00:00+00:00"],
    )


def test_build_risk_context_fills_every_field(lake, state):
    _fill(state, "o1", "A.US", "buy", 5, "2026-01-05")
    _fill(state, "o2", "A.US", "sell", 5, "2026-02-02")  # flat again
    _fill(state, "o3", "A.US", "buy", 3, "2026-03-02")  # re-opened
    _fill(state, "o4", "A.US", "buy", 2, "2026-03-09")  # added to
    _fill(state, "o5", "B.US", "buy", 1, "2026-03-03")
    for day, value in (("2026-03-27", 1_000.0), ("2026-03-30", 1_010.0), ("2026-04-02", 5.0)):
        state.execute(
            "INSERT INTO portfolio_snapshots (taken_at, as_of, cash, positions_json, total_value)"
            " VALUES (?, ?, 0, '{}', ?)",
            [f"{day}T21:00:00+00:00", day, value],
        )
    portfolio = Portfolio(cash=100.0, positions={"A.US": 5.0, "B.US": 1.0})
    ctx = build_risk_context(
        lake,
        state,
        portfolio,
        {"A.US": 12.0, "B.US": 55.0},
        AS_OF,
        policy=RiskPolicy(cash_buffer_fraction=0.1),
        cost_model=CostModelSettings.realistic(),
        volumes={"A.US": 1_000.0},
    )
    assert ctx.portfolio is portfolio
    assert ctx.policy.cash_buffer_fraction == 0.1
    assert ctx.asset_classes == {"A.US": "equity", "B.US": "equity"}
    assert ctx.sectors == {"A.US": "Technology"}
    assert set(ctx.history) == {"A.US", "B.US"}
    assert len(ctx.history["A.US"]) == 260
    assert ctx.equity_curve == [(date(2026, 3, 27), 1_000.0), (date(2026, 3, 30), 1_010.0)]
    assert ctx.entry_dates == {"A.US": date(2026, 3, 2), "B.US": date(2026, 3, 3)}
    assert ctx.cost_model is not None
    assert ctx.volumes == {"A.US": 1_000.0}
    assert ctx.as_of == AS_OF
