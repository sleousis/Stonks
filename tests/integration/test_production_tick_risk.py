"""The risk layer sits between ``strategy.decide`` and the broker in run_tick."""

from __future__ import annotations

import json
from datetime import date

import pytest

from stonks.core.protocols import SurvivalReport
from stonks.production.risk import RiskPolicy
from stonks.production.tick import TickSettings, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status

AS_OF = date(2026, 3, 20)


@pytest.fixture
def tick_env(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    sid = registry.register(
        BuyAndHold({"ticker": "UP.US", "allocation": 1.0}),
        reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
    )
    seed_status(registry, sid, "active")
    yield lake_trending, state, registry
    state.close()


def _summary(state, tick_id):
    row = state.sql("SELECT summary_json FROM tick_runs WHERE id = ?", [tick_id])[0]
    return json.loads(row["summary_json"])


def test_tick_clips_buy_to_max_weight_per_ticker(tick_env):
    lake, state, registry = tick_env
    settings = TickSettings(
        universe=["UP.US"],
        initial_cash=10_000.0,
        risk=RiskPolicy(max_weight_per_ticker=0.3),
    )
    result = run_tick(state, lake, registry, settings, as_of=AS_OF)

    assert result.fills == 1
    fill = state.sql("SELECT quantity, price FROM fills")[0]
    assert fill["quantity"] * fill["price"] == pytest.approx(3_000.0)
    snap = state.sql("SELECT cash FROM portfolio_snapshots")[0]
    assert snap["cash"] == pytest.approx(7_000.0)

    summary = _summary(state, result.tick_id)
    [adj] = summary["risk_adjustments"]
    assert adj["ticker"] == "UP.US"
    assert adj["rule"] == "max_weight_per_ticker"


def test_tick_where_risk_drops_every_order_places_nothing(tick_env):
    lake, state, registry = tick_env
    settings = TickSettings(
        universe=["UP.US"],
        initial_cash=10_000.0,
        risk=RiskPolicy(max_open_positions=0),
    )
    result = run_tick(state, lake, registry, settings, as_of=AS_OF)

    assert result.status == "ok"
    assert result.orders_placed == 0
    assert state.count_rows("orders") == 0
    # the snapshot still lands so the portfolio ledger stays continuous
    assert state.count_rows("portfolio_snapshots") == 1
    assert _summary(state, result.tick_id)["risk_adjustments"][0]["rule"] == "max_open_positions"


def test_dry_run_applies_risk_too(tick_env):
    lake, state, registry = tick_env
    settings = TickSettings(
        universe=["UP.US"],
        initial_cash=10_000.0,
        risk=RiskPolicy(max_open_positions=0),
    )
    result = run_tick(state, lake, registry, settings, as_of=AS_OF, dry_run=True)
    assert result.orders_placed == 0
    assert _summary(state, result.tick_id)["risk_adjustments"]


class RotateByEquity(BuyAndHold):
    """Sells every other holding and buys the target sized by total equity,
    i.e. it counts on the sale proceeds. Module scope for the registry."""

    def decide(self, my_picks, portfolio, prices, as_of):
        from stonks.core.types import Order

        target = self.params["ticker"]
        orders = [
            Order(client_id=f"s:{t}", ticker=t, side="sell", quantity=q)
            for t, q in portfolio.positions.items()
            if t != target and q > 0
        ]
        equity = portfolio.total_value(prices)
        orders.append(
            Order(client_id="b", ticker=target, side="buy", quantity=equity / prices[target])
        )
        return orders


class MarginBroker:
    """Rejects sells, fills buys without checking cash (like a margin account)."""

    def __init__(self) -> None:
        self.placed: list = []

    def place_order(self, order):
        from datetime import UTC, datetime

        from stonks.core.types import Fill

        self.placed.append(order)
        if order.side == "sell":
            return None
        return Fill(
            order_client_id=order.client_id,
            ticker=order.ticker,
            quantity=order.quantity,
            price=1.0,
            fee=0.0,
            filled_at=datetime.now(UTC),
            side=order.side,
        )


def test_buys_are_not_funded_by_sells_that_failed(tmp_path, lake_trending, monkeypatch):
    import stonks.production.tick as tick_mod

    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    try:
        registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
        sid = registry.register(
            RotateByEquity({"ticker": "UP.US", "allocation": 1.0}),
            reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
        )
        seed_status(registry, sid, "active")
        # fully invested in DOWN.US, no cash
        state.execute("INSERT INTO tick_runs (id, started_at, status) VALUES ('t0', 'x', 'ok')")
        state.execute(
            "INSERT INTO portfolio_snapshots (tick_id, taken_at, cash, positions_json, "
            "total_value) VALUES ('t0', 'x', 0, ?, 0)",
            [json.dumps({"DOWN.US": 100.0})],
        )
        broker = MarginBroker()
        monkeypatch.setattr(tick_mod, "_build_broker", lambda *a, **k: broker)
        settings = TickSettings(universe=["UP.US", "DOWN.US"], initial_cash=10_000.0)

        run_tick(state, lake_trending, registry, settings, as_of=AS_OF)

        assert [(o.side, o.ticker) for o in broker.placed] == [("sell", "DOWN.US")]
    finally:
        state.close()


def test_buys_use_proceeds_of_sells_that_filled(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    try:
        registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
        sid = registry.register(
            RotateByEquity({"ticker": "UP.US", "allocation": 1.0}),
            reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
        )
        seed_status(registry, sid, "active")
        state.execute("INSERT INTO tick_runs (id, started_at, status) VALUES ('t0', 'x', 'ok')")
        state.execute(
            "INSERT INTO portfolio_snapshots (tick_id, taken_at, cash, positions_json, "
            "total_value) VALUES ('t0', 'x', 0, ?, 0)",
            [json.dumps({"DOWN.US": 100.0})],
        )
        settings = TickSettings(universe=["UP.US", "DOWN.US"], initial_cash=10_000.0)
        result = run_tick(state, lake_trending, registry, settings, as_of=AS_OF)
        assert result.fills == 2
        sides = [r["side"] for r in state.sql("SELECT side FROM orders ORDER BY created_at")]
        assert sorted(sides) == ["buy", "sell"]
    finally:
        state.close()


def test_tick_without_risk_limits_has_empty_adjustments(tick_env):
    lake, state, registry = tick_env
    settings = TickSettings(universe=["UP.US"], initial_cash=10_000.0)
    result = run_tick(state, lake, registry, settings, as_of=AS_OF)
    assert _summary(state, result.tick_id)["risk_adjustments"] == []
