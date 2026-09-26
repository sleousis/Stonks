"""BL-32: the tick records each order's decision, the simulated fill its
arrival price, and the tca hook fills the next session's benchmark prices,
so shortfall can be read back from the ledger."""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from stonks.backtest.costs import AssetClassCosts, CostModelSettings, Trade
from stonks.core.protocols import SurvivalReport
from stonks.execution.reconcile import reconcile_orders
from stonks.production.tca import load_order_tca, refresh_benchmarks, summarize
from stonks.production.tick import TickSettings, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status
from tests.integration.test_reconcile import FakeStateBroker, insert_order

AS_OF = date(2026, 3, 20)
NEXT = date(2026, 3, 23)
MODEL = CostModelSettings(default=AssetClassCosts(half_spread_bps=5.0, fee_bps=1.0))


def _close(day: date) -> float:
    days = list(pd.bdate_range(start="2025-10-01", end="2026-04-01").date)
    return 100.0 + 100.0 * days.index(day) / (len(days) - 1)


@pytest.fixture
def env(tmp_path):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    sid = registry.register(
        BuyAndHold({"ticker": "UP.US", "allocation": 0.5}),
        reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
    )
    seed_status(registry, sid, "active")
    yield state, registry, sid
    state.close()


def test_tick_records_the_decision_and_the_arrival(env, lake_trending):
    state, registry, sid = env
    run_tick(
        state,
        lake_trending,
        registry,
        TickSettings(universe=["UP.US"], costs=MODEL, shadow_enabled=False),
        as_of=AS_OF,
    )
    [order] = state.sql("SELECT * FROM orders")
    assert order["decision_price"] == pytest.approx(_close(AS_OF))
    assert order["decided_at"].startswith("2026-03-20")
    context = json.loads(order["decision_context_json"])
    assert context["trigger"] == "signal"
    assert context["strategy_id"] == sid
    assert context["constructor"] == "single_winner"
    expected = MODEL.build().cost(
        Trade(
            ticker="UP.US",
            side="buy",
            quantity=order["quantity"],
            price=_close(AS_OF),
            bar_volume=1_000_000.0,
        )
    )
    fee_bps = expected.fee / (order["quantity"] * _close(AS_OF)) * 1e4
    assert order["expected_cost_bps"] == pytest.approx(5.0 + fee_bps)
    [fill] = state.sql("SELECT * FROM fills")
    # the simulated broker fills at the close: that close is the arrival
    assert fill["arrival_price"] == pytest.approx(_close(AS_OF))


def test_the_hook_fills_benchmarks_and_shortfall_matches_the_model(env, lake_trending):
    state, registry, sid = env
    run_tick(
        state,
        lake_trending,
        registry,
        TickSettings(universe=["UP.US"], costs=MODEL, shadow_enabled=False),
        as_of=AS_OF,
    )
    [order] = state.sql("SELECT * FROM orders")
    assert order["benchmark_price"] == pytest.approx(_close(NEXT))
    assert order["post_close_price"] == pytest.approx(_close(NEXT))
    [row] = load_order_tca(state, "pf_default")
    s = row.shortfall
    assert s.delay_bps == pytest.approx(0.0)
    assert s.impact_bps == pytest.approx(5.0)
    # the tick paid its model's cost exactly: the model is calibrated here
    assert s.is_bps == pytest.approx(order["expected_cost_bps"])
    # a backtest would have bought at the next open, above today's close
    assert s.convention_bps < 0
    [group] = summarize([row], "strategy")
    assert group.key == sid
    assert group.model_gap_bps == pytest.approx(0.0, abs=1e-9)


def test_a_dry_run_records_nothing(env, lake_trending):
    state, registry, _ = env
    run_tick(
        state,
        lake_trending,
        registry,
        TickSettings(universe=["UP.US"], costs=MODEL, shadow_enabled=False),
        as_of=AS_OF,
        dry_run=True,
    )
    assert state.sql("SELECT COUNT(*) FROM orders")[0][0] == 0


def _decided(state, client_id, *, status, day="2026-03-20", price=100.0, side="buy"):
    insert_order(state, client_id, status=status, ticker="UP.US", side=side)
    state.execute(
        "UPDATE orders SET decision_price = ?, decided_at = ? WHERE client_id = ?",
        [price, f"{day}T00:00:00+00:00", client_id],
    )


def test_refresh_prices_the_opportunity_cost_of_a_rejected_order(env, lake_trending):
    state, _, _ = env
    _decided(state, "rej", status="rejected", price=_close(AS_OF))
    assert refresh_benchmarks(state, lake_trending) == 1
    assert refresh_benchmarks(state, lake_trending) == 0  # idempotent
    [row] = load_order_tca(state, "pf_default")
    s = row.shortfall
    assert s.filled_quantity == 0
    # the price rose from the decision to the next close: a missed buy costs
    assert s.opportunity_cost == pytest.approx((_close(NEXT) - _close(AS_OF)) * 10.0)
    assert s.opportunity_bps > 0


def test_refresh_waits_for_the_next_bar(env, lake_trending):
    state, _, _ = env
    _decided(state, "late", status="pending", day="2026-04-01")
    assert refresh_benchmarks(state, lake_trending) == 0
    [row] = state.sql("SELECT benchmark_price FROM orders")
    assert row[0] is None


def test_external_fills_arrive_at_the_next_open(env, lake_trending):
    state, _, _ = env
    _decided(state, "ext", status="pending", price=_close(AS_OF))
    broker = FakeStateBroker()
    broker.set("ext", "filled", 10.0, _close(NEXT) + 0.1)
    reconcile_orders(broker, state)
    [fill] = state.sql("SELECT arrival_price FROM fills")
    assert fill[0] is None  # not known until the next bar is ingested
    refresh_benchmarks(state, lake_trending)
    [row] = load_order_tca(state, "pf_default")
    assert row.shortfall.arrival_price == pytest.approx(_close(NEXT))
    assert row.shortfall.impact_bps == pytest.approx(0.1 * 10 / (_close(AS_OF) * 10) * 1e4)


def test_reconcile_uses_a_known_benchmark_as_the_arrival(env):
    state, _, _ = env
    _decided(state, "ext2", status="pending")
    state.execute("UPDATE orders SET benchmark_price = 101.0 WHERE client_id = 'ext2'")
    broker = FakeStateBroker()
    broker.set("ext2", "filled", 10.0, 101.2)
    reconcile_orders(broker, state)
    [fill] = state.sql("SELECT arrival_price FROM fills")
    assert fill[0] == pytest.approx(101.0)


def test_golive_compares_live_shortfall_with_the_modelled_cost(env, lake_trending):
    from stonks.config import GoLivePolicy
    from stonks.production.golive import evaluate_golive

    state, registry, sid = env
    run_tick(
        state,
        lake_trending,
        registry,
        TickSettings(universe=["UP.US"], costs=MODEL, shadow_enabled=False),
        as_of=AS_OF,
    )
    report = evaluate_golive(state, registry, sid, GoLivePolicy())
    costs = report.costs
    assert costs["orders"] == 1
    assert costs["live_is_bps"] == pytest.approx(costs["modelled_bps"])
    assert costs["model_gap_bps"] == pytest.approx(0.0, abs=1e-9)
