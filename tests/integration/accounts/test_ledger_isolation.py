"""Tenant isolation of the ledger readers: with two portfolios (one of them
synced from a broker), no reader of ``portfolio_snapshots``, ``orders``,
``fills`` or the P&L mixes them, and the tick's own readers ignore sync
snapshots (``source = 'sync'``)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from stonks.accounts import (
    DEFAULT_PORTFOLIO_ID,
    PortfolioRepository,
    Role,
    Scope,
    UserRepository,
)
from stonks.app.context import AppContext
from stonks.app.portfolio import PortfolioService
from stonks.config import GoLivePolicy, LakeConfig, RegistryConfig, Settings, StateConfig
from stonks.core.protocols import SurvivalReport
from stonks.core.types import Portfolio
from stonks.execution.brokers.base import BrokerOrderState
from stonks.execution.reconcile import reconcile_orders
from stonks.production import tick as tick_mod
from stonks.production.golive import load_paper_period
from stonks.production.pnl import load_pnl
from stonks.production.risk import build_risk_context
from stonks.production.tick import TickSettings, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.reporting.data import build_report
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status

D1, D2, D3 = date(2026, 3, 16), date(2026, 3, 17), date(2026, 3, 18)


def _snapshot(state, pid, as_of, cash, positions, total, source="tick"):
    state.execute(
        "INSERT INTO portfolio_snapshots (tick_id, as_of, taken_at, cash, positions_json,"
        " total_value, portfolio_id, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            None,
            as_of.isoformat(),
            f"{as_of.isoformat()}T21:00:00+00:00",
            cash,
            json.dumps(positions),
            total,
            pid,
            source,
        ],
    )


def _order(state, pid, client_id, ticker, status="filled", strategy_id="bh"):
    state.execute(
        "INSERT INTO orders (client_id, tick_id, strategy_id, ticker, side, quantity,"
        " order_type, status, created_at, updated_at, portfolio_id)"
        " VALUES (?, NULL, ?, ?, 'buy', 10, 'market', ?, ?, ?, ?)",
        [client_id, strategy_id, ticker, status, f"{D1}T21:00:00", f"{D1}T21:00:00", pid],
    )
    if status == "filled":
        state.execute(
            "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at,"
            " portfolio_id) VALUES (?, ?, 10, 100, 0, ?, ?)",
            [client_id, ticker, f"{D1}T21:00:00", pid],
        )


@pytest.fixture
def two_books(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    registry.register(
        BuyAndHold({"ticker": "UP.US", "allocation": 0.5}),
        reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
        strategy_id="bh",
    )
    seed_status(registry, "bh", "active")
    user = UserRepository(state).create(display_name="Bob", role=Role.TRADER, actor="t")
    other = PortfolioRepository(state).create(Scope.for_user(user), name="Bob's broker").id

    # pf_default: two tick snapshots, one filled order on UP.US.
    _snapshot(state, DEFAULT_PORTFOLIO_ID, D1, 9_000.0, {"UP.US": 10}, 10_000.0)
    _snapshot(state, DEFAULT_PORTFOLIO_ID, D2, 9_000.0, {"UP.US": 10}, 10_100.0)
    _order(state, DEFAULT_PORTFOLIO_ID, "c-default", "UP.US")
    # the other portfolio: a later tick snapshot, a later sync snapshot, a
    # filled order and a pending one on other tickers.
    _snapshot(state, other, D2, 1_000.0, {"FLAT.US": 5}, 1_250.0)
    _snapshot(state, other, D3, 7.0, {"DOWN.US": 99}, 777.0, source="sync")
    _order(state, other, "c-other", "FLAT.US")
    _order(state, other, "c-other-pending", "DOWN.US", status="pending")
    yield state, registry, lake_trending, other
    state.close()


def test_pnl_reads_one_portfolio(two_books):
    state, _, _, other = two_books
    assert [r.total_value for r in load_pnl(state)] == [10_000.0, 10_100.0]
    # a portfolio's own P&L includes its sync snapshots (the account's truth)
    assert [r.total_value for r in load_pnl(state, portfolio_id=other)] == [1_250.0, 777.0]


def test_tick_readers_ignore_other_portfolios_and_sync_rows(two_books):
    state, _, _, other = two_books
    seed = tick_mod._load_or_seed_portfolio(state, 1.0, portfolio_id=DEFAULT_PORTFOLIO_ID)
    assert seed.positions == {"UP.US": 10}
    assert tick_mod._latest_snapshot_as_of(state, portfolio_id=DEFAULT_PORTFOLIO_ID) == D2
    # the tick's own ledger of the synced portfolio: the sync row is not its own
    assert tick_mod._latest_snapshot_as_of(state, portfolio_id=other) == D2
    assert tick_mod._load_or_seed_portfolio(state, 1.0, portfolio_id=other).positions == {
        "FLAT.US": 5
    }


def test_backdate_guard_only_looks_at_the_traded_portfolio(two_books):
    state, registry, lake, _ = two_books
    settings = TickSettings(universe=["UP.US", "FLAT.US"], shadow_enabled=False)
    # D2 is pf_default's latest snapshot; the other portfolio's D3 sync row
    # must not make a D2 tick of pf_default look backdated.
    result = run_tick(state, lake, registry, settings, as_of=D2)
    assert result.status in ("ok", "noop")
    with pytest.raises(tick_mod.BackdatedTickError):
        run_tick(state, lake, registry, settings, as_of=D1)


def test_report_and_golive_read_the_default_portfolio(two_books):
    state, registry, _, other = two_books
    report = build_report(state, registry, GoLivePolicy(), now=datetime(2026, 3, 19, tzinfo=UTC))
    assert report.positions is not None and report.positions.quantities == {"UP.US": 10.0}
    assert {o["ticker"] for o in report.orders} == {"UP.US"}
    assert {f["ticker"] for f in report.fills} == {"UP.US"}
    assert [r.total_value for r in report.portfolio] == [10_000.0, 10_100.0]
    period = load_paper_period(state, registry, "bh")
    assert period.trades == 1
    assert [r.total_value for r in period.rows] == [10_000.0, 10_100.0]
    mine = build_report(
        state,
        registry,
        GoLivePolicy(),
        now=datetime(2026, 3, 19, tzinfo=UTC),
        portfolio_id=other,
    )
    assert {o["ticker"] for o in mine.orders} == {"FLAT.US", "DOWN.US"}


def test_portfolio_service_reads_one_portfolio(two_books, tmp_path):
    state, _, lake, other = two_books
    settings = Settings(
        lake=LakeConfig(path=tmp_path / "lake.duckdb"),
        state=StateConfig(path=tmp_path / "state.sqlite"),
        registry=RegistryConfig(artifacts_dir=tmp_path / "artifacts"),
    )
    DuckDBLake(settings.lake.path).migrate()
    state.close()
    ctx = AppContext(settings)
    ctx.start()
    try:
        service = PortfolioService(ctx)
        view = service.current()
        assert [p.ticker for p in view.positions] == ["UP.US"]
        assert view.cash == 9_000.0
        assert view.positions[0].avg_cost == pytest.approx(100.0)
        assert service.snapshots(limit=10, offset=0).total == 2
        theirs = service.current(portfolio_id=other)
        assert [p.ticker for p in theirs.positions] == ["DOWN.US"]
        assert service.snapshots(limit=10, offset=0, portfolio_id=other).total == 2
    finally:
        ctx.close()


def test_risk_context_uses_one_portfolio(two_books):
    state, _, lake, _ = two_books
    ctx = build_risk_context(
        lake, state, Portfolio(cash=9_000.0, positions={"UP.US": 10}), {"UP.US": 100.0}, D2
    )
    assert [v for _, v in ctx.equity_curve] == [10_000.0, 10_100.0]
    assert set(ctx.entry_dates) == {"UP.US"}


class _Broker:
    """An order-state broker that knows only pf_default's orders."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def get_order_state(self, client_id: str) -> BrokerOrderState | None:
        self.asked.append(client_id)
        return None

    def place_order(self, order):  # pragma: no cover - never called
        raise AssertionError

    def reconcile(self):  # pragma: no cover
        return []

    def fetch_portfolio(self):  # pragma: no cover
        return Portfolio(cash=0.0)


def test_reconcile_only_touches_its_portfolio(two_books):
    state, _, _, other = two_books
    broker = _Broker()
    summary = reconcile_orders(broker, state, portfolio_id=DEFAULT_PORTFOLIO_ID)
    assert summary.orders_checked == 0 and broker.asked == []
    status = state.sql("SELECT status FROM orders WHERE client_id = 'c-other-pending'")[0][0]
    assert status == "pending"  # not rejected as "never received" by another account
    summary = reconcile_orders(broker, state, portfolio_id=other)
    assert broker.asked == ["c-other-pending"]
