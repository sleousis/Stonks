"""Roadmap 20.1 at an external broker: the tick never trades what a person
bought by hand in the default portfolio's account.

The default portfolio trades at an external broker (``[brokers].kind``).
A person bought FLAT by hand (a manual order, filled at the broker). The
strategy's target book leaves FLAT out, which would sell a holding the
book owns, but a manual holding is the person's and must be kept, as in a
simulated book and in a connected account."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from stonks.core.protocols import SurvivalReport
from stonks.core.types import Fill, Order
from stonks.execution.brokers import AlpacaBroker
from stonks.portfolio.settings import ConstructionSettings
from stonks.production.tick import TickSettings, _record_fill, _record_order, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status
from tests.unit.test_alpaca_broker import FakeClient

AS_OF = date(2026, 3, 20)
SETTINGS = TickSettings(
    universe=["UP.US", "FLAT.US"],
    initial_cash=10_000.0,
    broker_kind="alpaca",
    construction=ConstructionSettings(method="equal_weight_top_n"),
)


@pytest.fixture
def env(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    sid = registry.register(
        BuyAndHold({"ticker": "UP.US", "allocation": 0.5}),
        reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
    )
    seed_status(registry, sid, "active")
    client = FakeClient()
    client.account = {"cash": "10000", "currency": "USD", "status": "ACTIVE"}
    client.positions = [{"symbol": "FLAT", "qty": "2", "side": "long", "asset_class": "us_equity"}]

    def factory(portfolio):
        return AlpacaBroker(client, max_retries=0, retry_backoff_seconds=0.0, sleep=lambda s: 0)

    yield lake_trending, state, registry, client, factory
    state.close()


def _record_manual_buy(state: SqliteState, origin: str) -> None:
    cid = "manual:pf_default:mine"
    order = Order(client_id=cid, ticker="FLAT.US", side="buy", quantity=2.0)
    _record_order(state, order, status="filled", portfolio_id="pf_default")
    state.execute("UPDATE orders SET origin = ? WHERE client_id = ?", [origin, cid])
    _record_fill(
        state,
        Fill(
            order_client_id=cid,
            ticker="FLAT.US",
            side="buy",
            quantity=2.0,
            price=50.0,
            fee=0.0,
            filled_at=datetime(2026, 3, 18, 15, 0, tzinfo=UTC),
        ),
        portfolio_id="pf_default",
    )


def _sold(client) -> list[str]:
    return [str(o.symbol) for o in client.submitted if str(o.side).lower().endswith("sell")]


def test_the_tick_keeps_a_manual_holding_at_an_external_broker(env):
    lake, state, registry, client, factory = env
    _record_manual_buy(state, "manual")
    run_tick(state, lake, registry, SETTINGS, as_of=AS_OF, broker_factory=factory)
    assert "FLAT" not in _sold(client)


def test_the_same_holding_bought_by_a_strategy_is_sold_at_an_external_broker(env):
    """Control: the holding is only safe because it is manual."""
    lake, state, registry, client, factory = env
    _record_manual_buy(state, "strategy")
    run_tick(state, lake, registry, SETTINGS, as_of=AS_OF, broker_factory=factory)
    assert "FLAT" in _sold(client)
