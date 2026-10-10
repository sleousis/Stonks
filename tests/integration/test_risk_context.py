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


# ---- the cash-flow-adjusted curve (deposits and withdrawals taken out) ----


def _snap(state, day: str, value: float, pf: str = "pf_default") -> None:
    state.execute(
        "INSERT INTO portfolio_snapshots (taken_at, as_of, cash, positions_json, total_value,"
        " portfolio_id) VALUES (?, ?, ?, '{}', ?, ?)",
        [f"{day}T21:00:00+00:00", day, value, value, pf],
    )


def _flow(state, day: str, kind: str, amount: float, pf: str = "pf_default") -> None:
    state.execute(
        "INSERT INTO portfolio_cash_flows (portfolio_id, flow_date, kind, amount, created_at,"
        " created_by) VALUES (?, ?, ?, ?, ?, 'test')",
        [pf, day, kind, amount, f"{day}T12:00:00+00:00"],
    )


def _rule(name: str):
    from stonks.production.rules import registered_rules

    [rule] = [r for r in registered_rules() if r.name == name]
    return rule


def _buy():
    from stonks.core.types import Order

    return Order(client_id="b1", ticker="A.US", side="buy", quantity=10.0)


def _policy(**rules):
    from stonks.production.rules.settings import RuleSettings

    return RiskPolicy(rules=RuleSettings.model_validate(rules))


def test_a_deposit_after_a_10pct_loss_still_shows_a_10pct_drawdown(lake, state):
    _snap(state, "2026-03-26", 1_000.0)
    _snap(state, "2026-03-27", 900.0)  # 10% loss
    _flow(state, "2026-03-30", "deposit", 1_000.0)
    _snap(state, "2026-03-30", 1_900.0)  # the deposit's own snapshot
    pol = _policy(drawdown_scaling={"schedule": [(0.05, 0.5)]})
    ctx = build_risk_context(
        lake, state, Portfolio(cash=1_900.0), {"A.US": 12.0}, AS_OF, policy=pol
    )
    values = [v for _, v in ctx.equity_curve]
    assert 1 - values[-1] / max(values) == pytest.approx(0.10)
    kept, adj = _rule("drawdown_scaling").apply([_buy()], ctx)
    assert kept[0].quantity == pytest.approx(5.0)
    assert "10.00%" in adj[0].reason


def test_the_deposit_day_without_a_snapshot_lines_up_with_todays_value(lake, state):
    _snap(state, "2026-03-27", 1_000.0)
    _snap(state, "2026-03-30", 900.0)
    _flow(state, "2026-03-31", "deposit", 1_000.0)  # landed today, before the tick
    pol = _policy(drawdown_scaling={"schedule": [(0.05, 0.5)]})
    ctx = build_risk_context(
        lake, state, Portfolio(cash=1_900.0), {"A.US": 12.0}, AS_OF, policy=pol
    )
    assert ctx.equity_curve[-1][1] == pytest.approx(1_900.0)
    kept, _ = _rule("drawdown_scaling").apply([_buy()], ctx)
    assert kept[0].quantity == pytest.approx(5.0)


def test_a_withdrawal_does_not_trip_the_circuit_breaker(lake, state):
    _snap(state, "2026-03-26", 10_000.0)
    _snap(state, "2026-03-27", 10_100.0)
    _flow(state, "2026-03-30", "withdrawal", 5_000.0)
    _snap(state, "2026-03-30", 5_100.0)
    pol = _policy(circuit_breaker={"max_month_loss": 0.06, "max_drawdown_halt": 0.2})
    ctx = build_risk_context(
        lake, state, Portfolio(cash=5_100.0), {"A.US": 12.0}, AS_OF, policy=pol
    )
    kept, adj = _rule("circuit_breaker").apply([_buy()], ctx)
    assert adj == [] and kept[0].quantity == 10.0


def test_a_flow_in_another_currency_without_a_rate_keeps_the_raw_curve(lake, state, monkeypatch):
    from stonks.fx import FxRateMissing
    from stonks.insights import flows

    def missing(*_a, **_k):
        raise FxRateMissing("JPY", "USD", date(2026, 3, 30))

    monkeypatch.setattr(flows, "external_flows", missing)
    _snap(state, "2026-03-27", 1_000.0)
    _snap(state, "2026-03-30", 1_500.0)
    ctx = build_risk_context(lake, state, Portfolio(cash=1_500.0), {}, AS_OF)
    assert ctx.equity_curve == [(date(2026, 3, 27), 1_000.0), (date(2026, 3, 30), 1_500.0)]
