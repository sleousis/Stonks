"""Roadmap 2.6: the tick trading through an opt-in external broker (Alpaca).

Hermetic: every test drives ``AlpacaBroker`` over the fake alpaca-py client
from the adapter's unit tests. No network, no keys.
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

import stonks.production.tick as tick_mod
from stonks.config import Settings
from stonks.core.protocols import SurvivalReport
from stonks.core.types import Portfolio
from stonks.execution.brokers import AlpacaBroker
from stonks.execution.brokers.base import BrokerError
from stonks.production.settings_builder import build_tick_runtime
from stonks.production.tick import TickSettings, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status
from tests.unit.test_alpaca_broker import FakeClient, raw_order

AS_OF = date(2026, 3, 20)
SETTINGS = TickSettings(universe=["UP.US"], initial_cash=10_000.0, broker_kind="alpaca")


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
    brokers: list[AlpacaBroker] = []

    def factory(portfolio):
        # A fresh adapter per tick, like a fresh process: nothing in memory.
        broker = AlpacaBroker(client, max_retries=0, retry_backoff_seconds=0.0, sleep=lambda s: 0)
        brokers.append(broker)
        return broker

    yield lake_trending, state, registry, sid, client, factory, brokers
    state.close()


def _tick(env, as_of=AS_OF, **kw):
    lake, state, registry, _, _, factory, _ = env
    return run_tick(state, lake, registry, SETTINGS, as_of=as_of, broker_factory=factory, **kw)


def _orders(state):
    return [dict(r) for r in state.sql("SELECT * FROM orders ORDER BY created_at")]


def test_unfilled_order_is_recorded_pending_not_rejected(env):
    _, state, _, sid, client, _, _ = env
    result = _tick(env)

    assert len(client.submitted) == 1
    [order] = _orders(state)
    assert (order["strategy_id"], order["ticker"], order["side"]) == (sid, "UP.US", "buy")
    assert order["status"] == "pending"
    assert order["broker_order_id"] == "broker-1"  # filled in by reconcile
    assert state.count_rows("fills") == 0
    assert result.status == "ok"
    assert result.orders_placed == 1
    assert result.fills == 0


def test_portfolio_comes_from_the_broker_and_is_snapshotted(env):
    _, state, _, _, client, _, _ = env
    client.account = {"cash": "4321", "currency": "USD", "status": "ACTIVE"}
    client.positions = [{"symbol": "FLAT", "qty": "2", "side": "long", "asset_class": "us_equity"}]
    _tick(env)

    snap = state.sql("SELECT cash, positions_json, total_value FROM portfolio_snapshots")[0]
    assert snap["cash"] == pytest.approx(4321.0)
    assert snap["positions_json"] == '{"FLAT.US": 2.0}'
    assert snap["total_value"] == pytest.approx(4321.0 + 2 * 50.0)
    # BuyAndHold sized its buy off the broker's cash, not initial_cash
    qty = float(client.submitted[0].qty)
    assert qty * 190 < 4321 * 0.5 + 1


def test_immediate_fill_is_booked_once_through_reconcile(env, monkeypatch):
    _, state, _, _, client, _, brokers = env
    client.next_status = "filled"
    client.next_fill_price = "190.5"
    monkeypatch.setattr(
        AlpacaBroker, "reconcile", lambda self: pytest.fail("fills must come from reconcile_orders")
    )
    result = _tick(env)

    [order] = _orders(state)
    assert order["status"] == "filled"
    fills = state.sql("SELECT quantity, price FROM fills")
    assert len(fills) == 1
    assert fills[0]["price"] == pytest.approx(190.5)
    assert fills[0]["quantity"] == pytest.approx(float(client.submitted[0].qty))
    assert result.fills == 1


def test_reconcile_runs_before_deciding(env, monkeypatch):
    _, state, _, _, client, _, _ = env
    calls: list[str] = []
    real_reconcile = tick_mod.reconcile_orders
    real_fetch = AlpacaBroker.fetch_portfolio

    def spy_reconcile(broker, st, **kw):
        calls.append("reconcile")
        return real_reconcile(broker, st, **kw)

    def spy_fetch(self):
        calls.append("fetch_portfolio")
        return real_fetch(self)

    monkeypatch.setattr(tick_mod, "reconcile_orders", spy_reconcile)
    monkeypatch.setattr(AlpacaBroker, "fetch_portfolio", spy_fetch)
    _tick(env)
    assert calls[:2] == ["reconcile", "fetch_portfolio"]


def test_pending_order_from_an_earlier_tick_is_reconciled_first(env):
    _, state, _, sid, client, _, _ = env
    cid = f"2026-03-19:{sid}:UP.US:buy"
    state.execute("INSERT INTO tick_runs (id, started_at, status) VALUES ('t0', 'x', 'ok')")
    state.execute(
        "INSERT INTO orders (client_id, tick_id, strategy_id, ticker, side, quantity, order_type,"
        " status, created_at, updated_at) VALUES (?, 't0', ?, 'UP.US', 'buy', 10, 'market',"
        " 'pending', '2026-03-19', '2026-03-19')",
        [cid, sid],
    )
    client.orders[cid] = raw_order(
        cid, qty="10", filled_qty="10", filled_avg_price="188", status="filled", order_id="b-0"
    )
    _tick(env)

    row = state.sql("SELECT status, broker_order_id FROM orders WHERE client_id = ?", [cid])[0]
    assert (row["status"], row["broker_order_id"]) == ("filled", "b-0")
    fills = state.sql("SELECT quantity, price FROM fills WHERE order_client_id = ?", [cid])
    assert [(f["quantity"], f["price"]) for f in fills] == [(10.0, 188.0)]


def test_rerun_same_day_does_not_resubmit_a_pending_order(env):
    _, state, _, _, client, _, _ = env
    _tick(env)
    _tick(env)
    assert len(client.submitted) == 1
    assert len(_orders(state)) == 1
    assert _orders(state)[0]["status"] == "pending"


def test_rerun_after_fill_never_double_books(env):
    _, state, _, _, client, _, _ = env
    client.next_status = "filled"
    client.next_fill_price = "190"
    _tick(env)
    _tick(env)  # fresh adapter: its in-memory booking is empty
    assert len(client.submitted) == 1
    assert state.count_rows("fills") == 1


def test_pre_trade_rejection_is_recorded_rejected(env):
    _, state, _, _, client, _, _ = env
    client.account = {"cash": "10000", "currency": "USD", "status": "ACCOUNT_CLOSED"}
    result = _tick(env)
    assert [o["status"] for o in _orders(state)] == ["rejected"]
    assert client.submitted == []
    assert result.status == "ok"


def test_dry_run_with_alpaca_writes_and_submits_nothing(env, monkeypatch):
    _, state, _, _, client, _, _ = env
    monkeypatch.setattr(
        tick_mod, "reconcile_orders", lambda *a, **k: pytest.fail("dry-run must not reconcile")
    )
    result = _tick(env, dry_run=True)
    assert result.orders_placed == 1
    assert client.submitted == []
    assert state.count_rows("orders") == 0
    assert state.count_rows("portfolio_snapshots") == 0


def test_alpaca_kind_without_a_broker_factory_fails_loudly(env):
    lake, state, registry, *_ = env
    with pytest.raises(ValueError, match="broker_factory"):
        run_tick(state, lake, registry, SETTINGS, as_of=AS_OF)


def test_simulated_tick_never_reconciles(env, monkeypatch):
    lake, state, registry, *_ = env
    monkeypatch.setattr(
        tick_mod, "reconcile_orders", lambda *a, **k: pytest.fail("simulated must not reconcile")
    )
    result = run_tick(state, lake, registry, TickSettings(universe=["UP.US"]), as_of=AS_OF)
    assert result.fills == 1


def test_previously_rejected_order_is_retried(env):
    lake, state, registry, sid, *_ = env
    cid = f"{AS_OF.isoformat()}:{sid}:UP.US:buy"
    state.execute("INSERT INTO tick_runs (id, started_at, status) VALUES ('t0', 'x', 'ok')")
    state.execute(
        "INSERT INTO orders (client_id, tick_id, strategy_id, ticker, side, quantity, order_type,"
        " status, created_at, updated_at) VALUES (?, 't0', ?, 'UP.US', 'buy', 10, 'market',"
        " 'rejected', 'x', 'x')",
        [cid, sid],
    )
    result = run_tick(state, lake, registry, TickSettings(universe=["UP.US"]), as_of=AS_OF)
    assert result.fills == 1
    assert state.sql("SELECT status FROM orders")[0]["status"] == "filled"


# ---- builder ----------------------------------------------------------------


@pytest.fixture
def no_alpaca_env(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)


def test_builder_leaves_simulated_ticks_without_a_factory(no_alpaca_env):
    assert build_tick_runtime(Settings(), ["UP.US"]).broker_factory is None


def test_builder_alpaca_factory_connects_lazily_with_env_keys(no_alpaca_env, monkeypatch):
    runtime = build_tick_runtime(Settings(brokers={"kind": "alpaca"}), ["UP.US"])
    assert runtime.settings.broker_kind == "alpaca"
    assert runtime.broker_factory is not None
    with pytest.raises(BrokerError, match="ALPACA_API_KEY"):
        runtime.broker_factory(None)

    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setattr(
        "stonks.execution.brokers.alpaca.TradingClient", lambda *a, **k: FakeClient()
    )
    runtime = build_tick_runtime(Settings(brokers={"kind": "alpaca"}), ["UP.US"])
    assert isinstance(runtime.broker_factory(None), AlpacaBroker)


# ---- self-review: broker-side rejections, open orders, late positions -------


def test_broker_side_rejection_is_booked_and_notified(env):
    from tests.integration.test_production_tick_notify import Recorder

    _, state, _, _, client, _, _ = env
    client.next_status = "rejected"
    recorder = Recorder()
    _tick(env, notifier=recorder)
    assert [o["status"] for o in _orders(state)] == ["rejected"]
    assert [n.title for n in recorder.sent] == ["orders rejected"]


def test_open_order_from_an_earlier_day_blocks_a_second_order(env):
    _, state, _, sid, client, _, _ = env
    cid = f"2026-03-19:{sid}:UP.US:buy"
    state.execute("INSERT INTO tick_runs (id, started_at, status) VALUES ('t0', 'x', 'ok')")
    state.execute(
        "INSERT INTO orders (client_id, tick_id, strategy_id, ticker, side, quantity, order_type,"
        " status, created_at, updated_at) VALUES (?, 't0', ?, 'UP.US', 'buy', 10, 'market',"
        " 'pending', '2026-03-19', '2026-03-19')",
        [cid, sid],
    )
    client.orders[cid] = raw_order(cid, symbol="UP", qty="10", status="new", order_id="b-0")

    result = _tick(env)

    assert client.submitted == []  # a GTC buy is still working: don't buy twice
    assert state.count_rows("orders") == 1
    summary = json.loads(
        state.sql("SELECT summary_json FROM tick_runs WHERE id = ?", [result.tick_id])[0][0]
    )
    assert summary["open_order_conflicts"] == [{"ticker": "UP.US", "side": "buy"}]


def test_snapshot_prices_positions_that_appear_after_trading(env, monkeypatch):
    _, state, _, _, _, _, _ = env
    portfolios = iter(
        [Portfolio(cash=10_000.0), Portfolio(cash=9_000.0, positions={"DOWN.US": 2.0})]
    )
    monkeypatch.setattr(AlpacaBroker, "fetch_portfolio", lambda self: next(portfolios))
    _tick(env)
    snap = state.sql("SELECT total_value FROM portfolio_snapshots")[0]
    days = list(pd.bdate_range(start="2025-10-01", end="2026-04-01").date)
    down = 100.0 - 40.0 * days.index(AS_OF) / (len(days) - 1)
    assert snap["total_value"] == pytest.approx(9_000.0 + 2 * down)


# ---- roadmap 8.5: no crash window between submit and the ledger -------------

NEXT_DAY = date(2026, 3, 23)


class Crash(BaseException):
    """Simulated process death: nothing in the tick may catch it."""


def _cid(sid, as_of=AS_OF):
    return f"{as_of.isoformat()}:{sid}:UP.US:buy"


def test_order_row_is_committed_pending_before_submit(env, tmp_path):
    _, state, _, sid, client, _, _ = env
    real_submit = client.submit_order
    seen: list[list[dict]] = []

    def spy(order_data):
        # A second connection only sees committed rows.
        other = SqliteState(tmp_path / "state.sqlite")
        try:
            rows = other.sql("SELECT client_id, status, broker_order_id FROM orders")
            seen.append([dict(r) for r in rows])
        finally:
            other.close()
        return real_submit(order_data)

    client.submit_order = spy
    _tick(env)

    assert seen == [[{"client_id": _cid(sid), "status": "pending", "broker_order_id": None}]]
    [row] = _orders(state)
    assert (row["status"], row["broker_order_id"]) == ("pending", "broker-1")


def test_crash_right_after_submit_is_recovered_by_the_next_tick(env):
    _, state, _, sid, client, _, _ = env
    real_submit = client.submit_order

    def submit_then_die(order_data):
        real_submit(order_data)
        raise Crash

    client.submit_order = submit_then_die
    with pytest.raises(Crash):
        _tick(env)
    [row] = _orders(state)
    assert (row["client_id"], row["status"], row["broker_order_id"]) == (
        _cid(sid),
        "pending",
        None,
    )

    # The broker filled it meanwhile; the rerun must book it, never resubmit.
    client.submit_order = real_submit
    client.orders[_cid(sid)].update(
        status="filled", filled_qty=client.orders[_cid(sid)]["qty"], filled_avg_price="190"
    )
    _tick(env)

    assert len(client.submitted) == 1
    [row] = _orders(state)
    assert (row["status"], row["broker_order_id"]) == ("filled", "broker-1")
    assert state.count_rows("fills") == 1


def test_crash_right_before_submit_is_rejected_on_the_next_day(env):
    _, state, _, sid, client, _, _ = env
    real_submit = client.submit_order

    def die(order_data):
        raise Crash

    client.submit_order = die
    with pytest.raises(Crash):
        _tick(env)
    assert [r["status"] for r in _orders(state)] == ["pending"]

    client.submit_order = real_submit
    _tick(env, as_of=NEXT_DAY)

    stale = state.sql("SELECT * FROM orders WHERE client_id = ?", [_cid(sid)])[0]
    assert stale["status"] == "rejected"
    assert "not found at the broker" in stale["status_reason"]
    fresh = state.sql("SELECT * FROM orders WHERE client_id = ?", [_cid(sid, NEXT_DAY)])[0]
    assert fresh["status"] == "pending"
    assert [o.client_order_id for o in client.submitted] == [_cid(sid, NEXT_DAY)]


def test_crash_right_before_submit_is_resubmitted_by_a_same_day_rerun(env):
    _, state, _, sid, client, _, _ = env
    real_submit = client.submit_order

    def die(order_data):
        raise Crash

    client.submit_order = die
    with pytest.raises(Crash):
        _tick(env)

    client.submit_order = real_submit
    _tick(env)

    assert [o.client_order_id for o in client.submitted] == [_cid(sid)]
    [row] = _orders(state)
    assert (row["status"], row["broker_order_id"], row["status_reason"]) == (
        "pending",
        "broker-1",
        None,
    )


def test_submit_error_leaves_the_row_pending_for_the_next_reconcile(env):
    from tests.unit.test_alpaca_broker import api_error

    _, state, _, sid, client, _, _ = env
    client.submit_errors.append(api_error(403, 40310000, "forbidden"))
    result = _tick(env)

    assert result.status == "partial"
    [row] = _orders(state)
    # The outcome is unknown (it may or may not have reached the broker):
    # the next tick's reconcile settles it by client id.
    assert row["status"] == "pending"


def test_pre_trade_rejection_records_its_reason(env):
    _, state, _, _, client, _, _ = env
    client.account = {"cash": "10000", "currency": "USD", "status": "ACCOUNT_CLOSED"}
    _tick(env)
    [row] = _orders(state)
    assert row["status"] == "rejected"
    assert "ACCOUNT_CLOSED" in row["status_reason"]


def test_the_second_risk_pass_never_places_a_forced_sell_twice(env):
    """An external broker's portfolio isn't updated by fills, so the buys'
    second risk pass sees the expired holding again and max_holding forces
    the same sell. Its client id is already in the ledger: it is skipped."""
    from stonks.config import RiskPolicy

    lake, state, registry, sid, client, factory, _ = env
    flat = registry.register(
        BuyAndHold({"ticker": "FLAT.US", "allocation": 0.2}),
        reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
        strategy_id="bh_flat",
    )
    seed_status(registry, flat, "active")
    seed_status(registry, sid, "retired")  # FLAT's strategy decides, and buys
    client.positions = [{"symbol": "UP", "qty": "10", "side": "long"}]
    state.execute(
        "INSERT INTO orders (client_id, strategy_id, ticker, side, quantity, order_type, status,"
        " created_at, updated_at) VALUES ('2026-03-02:x:UP.US:buy', ?, 'UP.US', 'buy', 10,"
        " 'market', 'filled', '2026-03-02T21:00:00', '2026-03-02T21:00:00')",
        [sid],
    )
    state.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at)"
        " VALUES ('2026-03-02:x:UP.US:buy', 'UP.US', 10, 150, 0, '2026-03-02T21:00:00')"
    )
    settings = TickSettings(
        universe=["UP.US", "FLAT.US"],
        initial_cash=10_000.0,
        broker_kind="alpaca",
        risk=RiskPolicy(rules={"max_holding": {"max_holding_bars": 2}}),
    )

    run_tick(state, lake, registry, settings, as_of=AS_OF, broker_factory=factory)

    sells = [o for o in client.submitted if "sell" in str(getattr(o, "side", "")).lower()]
    assert len(sells) == 1
    # the FLAT.US buy went through the second pass
    assert state.sql("SELECT 1 FROM orders WHERE side = 'buy' AND ticker = 'FLAT.US'")
    rows = state.sql("SELECT client_id FROM orders WHERE side = 'sell'")
    assert [r["client_id"] for r in rows] == ["2026-03-20:risk.max_holding:UP.US:sell"]


def test_be10_after_the_upgrade_the_live_default_book_still_orders(env):
    """Migration 022 subscribed ``pf_default`` in paper (010 made it a
    simulated portfolio). On an external-broker install the tick turns
    those system rows to auto, so the live account keeps trading."""
    from stonks.accounts import DEFAULT_PORTFOLIO_ID, Mode
    from stonks.accounts.default_book import ensure_default_subscription
    from stonks.production.tick import load_tick_plan

    lake, state, registry, sid, client, factory, _ = env
    sub_id = ensure_default_subscription(state, sid, Mode.PAPER)  # what 022 wrote
    plan = load_tick_plan(state, SETTINGS)
    [book] = [b for b in plan.books if b.portfolio_id == DEFAULT_PORTFOLIO_ID]
    assert book.mode == "auto"
    [row] = state.sql("SELECT mode FROM subscriptions WHERE id = ?", [sub_id])
    assert row["mode"] == "auto"
    [audit] = state.sql(
        "SELECT * FROM audit_log WHERE action = 'subscription.mode' AND target_id = ?", [sub_id]
    )
    assert audit["actor"] == "service:system"

    run_tick(state, lake, registry, SETTINGS, as_of=AS_OF, broker_factory=factory, plan=plan)
    assert len(client.submitted) == 1


def test_be10_an_owner_chosen_paper_row_stays_paper_and_a_dry_run_writes_nothing(env):
    from stonks.accounts import Mode, Scope, SubscriptionRepository
    from stonks.accounts.default_book import ensure_default_subscription
    from stonks.accounts.users import UserRepository
    from stonks.production.tick import load_tick_plan

    _, state, _, sid, _, _, _ = env
    sub_id = ensure_default_subscription(state, sid, Mode.PAPER)
    load_tick_plan(state, SETTINGS, dry_run=True)
    assert state.sql("SELECT mode FROM subscriptions WHERE id = ?", [sub_id])[0][0] == "paper"
    assert not state.sql("SELECT 1 FROM portfolios WHERE paper_of IS NOT NULL")

    owner = Scope.for_user(UserRepository(state).get("usr_owner"))
    subs = SubscriptionRepository(state)
    subs.set_mode(owner, sub_id, Mode.NOTIFY)
    subs.set_mode(owner, sub_id, Mode.PAPER)
    load_tick_plan(state, SETTINGS)
    assert state.sql("SELECT mode FROM subscriptions WHERE id = ?", [sub_id])[0][0] == "paper"


def test_be10_a_live_default_with_holdings_and_no_auto_strategy_alerts(env):
    from stonks.notify import Notifier
    from stonks.production.tick import load_tick_plan

    lake, state, registry, _, _, factory, _ = env
    state.execute(
        "INSERT INTO tick_runs (id, started_at, status) VALUES ('t0', '2026-03-19', 'ok')"
    )
    state.execute(
        "INSERT INTO portfolio_snapshots (tick_id, as_of, taken_at, cash, positions_json,"
        " total_value, portfolio_id) VALUES ('t0', '2026-03-19', '2026-03-19T21:00:00+00:00',"
        " 100.0, '{\"UP.US\": 5.0}', 900.0, 'pf_default')"
    )
    plan = load_tick_plan(state, SETTINGS)
    [notice] = plan.notices
    assert "pf_default" in notice

    class Capture(Notifier):
        def __init__(self) -> None:
            self.sent: list = []

        def _send(self, notification) -> None:
            self.sent.append(notification)

    capture = Capture()
    run_tick(state, lake, registry, SETTINGS, as_of=AS_OF, broker_factory=factory, plan=plan,
             notifier=capture)  # fmt: skip
    assert any(n.title == "default book unmanaged" for n in capture.sent)
