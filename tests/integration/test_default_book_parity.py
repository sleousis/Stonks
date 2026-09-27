"""The default book from subscriptions trades exactly like the old single
book (roadmap 15.5 follow-up).

Two identical installs trade the same days through the same status
changes. One runs the old plan (``TickPlan.default``: pf_default over every
active strategy). The other builds its books from subscriptions, where
pf_default is subscribed when a strategy turns active
(``accounts.default_book``). Orders, fills, snapshots and tick results
must be the same."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.accounts.default_book import ensure_default_subscription
from stonks.accounts.models import Mode
from stonks.core.protocols import SurvivalReport
from stonks.portfolio.settings import ConstructionSettings
from stonks.production.risk import RiskPolicy
from stonks.production.tick import TickSettings, load_tick_plan, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.strategies.examples.momentum import Momentum
from tests.fixtures.governance import seed_status

UNIVERSE = ["UP.US", "FLAT.US", "DOWN.US"]
DAYS = [
    date(2026, 3, 16),
    date(2026, 3, 18),
    date(2026, 3, 20),
    date(2026, 3, 24),
    date(2026, 3, 26),
]
STRATEGIES = {
    "mom": Momentum({"lookback_days": 5, "threshold": 0.0, "allocation": 0.5}),
    "bh_up": BuyAndHold({"ticker": "UP.US", "allocation": 0.4}),
    "bh_flat": BuyAndHold({"ticker": "FLAT.US", "allocation": 0.3}),
    "bh_down": BuyAndHold({"ticker": "DOWN.US", "allocation": 0.3}),
}
#: Status changes before the tick of day ``i``. No strategy that holds a
#: position is retired: there the books differ on purpose (BE-18: a
#: subscription book exits a retired strategy's holdings, the old single
#: book keeps them), which ``test_tick_modes`` covers.
PLAN = {
    0: [("mom", "active"), ("bh_up", "active")],
    1: [("bh_flat", "active")],
    2: [("bh_down", "active")],
    3: [],
    4: [],
}

ORDERS = (
    "SELECT client_id, strategy_id, ticker, side, quantity, order_type, status, status_reason,"
    " portfolio_id FROM orders ORDER BY client_id"
)
FILLS = "SELECT order_client_id, ticker, quantity, price, fee, portfolio_id FROM fills ORDER BY id"
SNAPSHOTS = (
    "SELECT as_of, cash, positions_json, total_value, portfolio_id FROM portfolio_snapshots"
    " ORDER BY id"
)


def _install(root, *, subscriptions: bool):
    state = SqliteState(root / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=root / "artifacts")
    reports = [SurvivalReport(test_id="oos", passed=True, metrics={})]
    for sid, strategy in STRATEGIES.items():
        registry.register(strategy, reports=reports, strategy_id=sid)
    return state, registry


@pytest.mark.parametrize(
    "settings",
    [
        TickSettings(universe=UNIVERSE, initial_cash=10_000.0),
        TickSettings(
            universe=UNIVERSE,
            initial_cash=25_000.0,
            threshold=0.01,
            risk=RiskPolicy(max_open_positions=2, cash_buffer_fraction=0.05),
        ),
        TickSettings(
            universe=UNIVERSE,
            initial_cash=10_000.0,
            construction=ConstructionSettings(method="equal_weight_top_n", params={"n": 2}),
        ),
    ],
    ids=["defaults", "risk", "targets"],
)
def test_subscription_books_match_the_old_single_book(tmp_path, lake_trending, settings):
    legacy = _install(tmp_path / "legacy", subscriptions=False)
    books = _install(tmp_path / "books", subscriptions=True)
    results = {"legacy": [], "books": []}
    for i, day in enumerate(DAYS):
        for name, (state, registry) in (("legacy", legacy), ("books", books)):
            for sid, status in PLAN.get(i, []):
                seed_status(registry, sid, status)
                if name == "books" and status == "active":
                    ensure_default_subscription(state, sid, Mode.PAPER)
            plan = load_tick_plan(state, settings) if name == "books" else None
            result = run_tick(state, lake_trending, registry, settings, as_of=day, plan=plan)
            results[name].append(
                (result.status, result.winner_strategy_id, result.orders_placed, result.fills)
            )
    assert results["books"] == results["legacy"]
    for query in (ORDERS, FILLS, SNAPSHOTS):
        rows = [[tuple(r) for r in state.sql(query)] for state, _ in (legacy, books)]
        assert rows[1] == rows[0], query
    assert any(n for _, _, n, _ in results["legacy"]), "the scenario must trade"
    for state, _ in (legacy, books):
        state.close()
