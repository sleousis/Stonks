"""Manual orders (roadmap 20.1): checks, execution, idempotency, cancel and
change, on a tmp lake and state."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pandas as pd
import pytest

from stonks.accounts import PortfolioRepository, Role, Scope, UserRepository
from stonks.config import LakeConfig, RiskPolicy, Settings, StateConfig
from stonks.core.types import Order, Portfolio
from stonks.execution.brokers.base import BrokerOrderState
from stonks.ingest.pipeline import _rows_to_df
from stonks.ingest.schemas import TickerProfile
from stonks.production.halts import trip_halt
from stonks.production.manual import (
    ManualBook,
    ManualOrder,
    ManualOrderNotFound,
    ManualOrderRefused,
    cancel_order,
    change_manual_order,
    manual_client_id,
    place_manual_order,
)
from stonks.production.settings_builder import build_tick_settings
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

NOW = datetime(2026, 4, 2, 15, 0, tzinfo=UTC)


@pytest.fixture
def lake(tmp_path):
    lk = DuckDBLake(tmp_path / "lake.duckdb")
    lk.migrate()
    days = pd.bdate_range("2026-03-02", "2026-04-01")
    rows = [
        {
            "ticker": t,
            "date": d.date(),
            "open": c,
            "high": c,
            "low": c,
            "close": c,
            "adj_close": c,
            "volume": 1_000_000,
        }
        for t, c in (("UP.US", 100.0), ("FLAT.US", 50.0))
        for d in days
    ]
    lk.upsert_prices(pd.DataFrame(rows))
    lk.upsert_instrument_profile(
        _rows_to_df(
            [
                TickerProfile(id="UP.US", name="Up", asset_class="equity"),
                TickerProfile(id="FLAT.US", name="Flat", asset_class="equity"),
            ]
        )
    )
    yield lk
    lk.close()


@pytest.fixture
def state(tmp_path):
    st = SqliteState(tmp_path / "state.sqlite")
    st.migrate()
    yield st
    st.close()


@pytest.fixture
def owner(state) -> str:
    return (
        UserRepository(state)
        .create(display_name="alice", role=Role.TRADER, actor="service:test", email="a@x.io")
        .id
    )


@pytest.fixture
def portfolio_id(state, owner) -> str:
    return (
        PortfolioRepository(state)
        .create(Scope(user_id=owner, role=Role.TRADER), name="Book", initial_cash=10_000.0)
        .id
    )


@pytest.fixture
def tick(tmp_path):
    settings = Settings(
        lake=LakeConfig(path=tmp_path / "lake.duckdb"),
        state=StateConfig(path=tmp_path / "state.sqlite"),
    )
    return build_tick_settings(settings, [])


def _book(portfolio_id: str, owner: str, **kw) -> ManualBook:
    return ManualBook(
        portfolio_id=portfolio_id,
        owner_id=owner,
        risk=kw.pop("risk", RiskPolicy()),
        initial_cash=10_000.0,
        **kw,
    )


def _order(portfolio_id: str, owner: str, **kw) -> ManualOrder:
    base = {
        "portfolio_id": portfolio_id,
        "ticker": "UP.US",
        "side": "buy",
        "quantity": 10.0,
        "reason": "my own idea",
        "actor": f"user:{owner}",
    }
    return ManualOrder(**(base | kw))


def test_market_buy_fills_at_latest_close_and_is_recorded_as_manual(
    state, lake, tick, portfolio_id, owner
):
    out = place_manual_order(
        state,
        lake,
        _order(portfolio_id, owner, client_key="k1"),
        _book(portfolio_id, owner),
        tick,
        now=NOW,
    )
    assert out.status == "filled"
    assert out.client_id == manual_client_id(portfolio_id, "k1")
    assert out.fill_price == pytest.approx(100.0)
    row = state.sql("SELECT * FROM orders WHERE client_id = ?", [out.client_id])[0]
    assert row["origin"] == "manual"
    assert row["strategy_id"] is None
    assert row["manual_reason"] == "my own idea"
    assert row["placed_by"] == f"user:{owner}"
    assert row["portfolio_id"] == portfolio_id
    assert json.loads(row["decision_context_json"])["trigger"] == "manual"
    fills = state.sql("SELECT * FROM fills WHERE order_client_id = ?", [out.client_id])
    assert len(fills) == 1 and fills[0]["portfolio_id"] == portfolio_id
    snap = state.sql(
        "SELECT * FROM portfolio_snapshots WHERE portfolio_id = ? ORDER BY id DESC",
        [portfolio_id],
    )[0]
    assert json.loads(snap["positions_json"]) == {"UP.US": 10.0}
    assert snap["cash"] == pytest.approx(10_000.0 - 1_000.0)
    audit = state.sql("SELECT action FROM audit_log WHERE target_id = ?", [out.client_id])
    assert [a["action"] for a in audit] == ["order.place"]


def test_same_client_key_is_idempotent(state, lake, tick, portfolio_id, owner):
    book = _book(portfolio_id, owner)
    first = place_manual_order(
        state, lake, _order(portfolio_id, owner, client_key="k"), book, tick, now=NOW
    )
    again = place_manual_order(
        state, lake, _order(portfolio_id, owner, client_key="k"), book, tick, now=NOW
    )
    assert again.duplicate and again.client_id == first.client_id and again.status == "filled"
    assert state.sql("SELECT COUNT(*) AS n FROM fills")[0]["n"] == 1


def test_same_key_for_another_order_is_refused(state, lake, tick, portfolio_id, owner):
    book = _book(portfolio_id, owner)
    place_manual_order(
        state, lake, _order(portfolio_id, owner, client_key="k"), book, tick, now=NOW
    )
    with pytest.raises(ManualOrderRefused, match="already used"):
        place_manual_order(
            state,
            lake,
            _order(portfolio_id, owner, client_key="k", ticker="FLAT.US"),
            book,
            tick,
            now=NOW,
        )


def test_preview_writes_nothing(state, lake, tick, portfolio_id, owner):
    out = place_manual_order(
        state,
        lake,
        _order(portfolio_id, owner),
        _book(portfolio_id, owner),
        tick,
        preview=True,
        now=NOW,
    )
    assert out.status == "preview" and out.quantity == 10.0 and out.reference_price == 100.0
    assert state.sql("SELECT COUNT(*) AS n FROM orders")[0]["n"] == 0


def test_kill_switch_refuses_every_order(state, lake, tick, portfolio_id, owner):
    trip_halt(
        state, "kill", reason="stop", actor="user:x", portfolio_id=portfolio_id, on=NOW.date()
    )
    with pytest.raises(ManualOrderRefused, match="halted"):
        place_manual_order(
            state, lake, _order(portfolio_id, owner), _book(portfolio_id, owner), tick, now=NOW
        )


def test_user_kill_switch_refuses_too(state, lake, tick, portfolio_id, owner):
    trip_halt(
        state, "kill", reason="stop", actor="user:x", scope="user", user_id=owner, on=NOW.date()
    )
    with pytest.raises(ManualOrderRefused, match="halted"):
        place_manual_order(
            state, lake, _order(portfolio_id, owner), _book(portfolio_id, owner), tick, now=NOW
        )


def test_buys_only_halt_lets_a_sell_of_a_holding_through(state, lake, tick, portfolio_id, owner):
    book = _book(portfolio_id, owner)
    place_manual_order(state, lake, _order(portfolio_id, owner), book, tick, now=NOW)
    trip_halt(
        state,
        "kill",
        reason="buys off",
        actor="user:x",
        portfolio_id=portfolio_id,
        halt="buys",
        on=NOW.date(),
    )
    with pytest.raises(ManualOrderRefused, match="halted"):
        place_manual_order(state, lake, _order(portfolio_id, owner), book, tick, now=NOW)
    sold = place_manual_order(
        state, lake, _order(portfolio_id, owner, side="sell", quantity=4.0), book, tick, now=NOW
    )
    assert sold.status == "filled" and sold.halt is not None


def test_risk_clip_refuses_unless_a_smaller_order_is_allowed(
    state, lake, tick, portfolio_id, owner
):
    book = _book(portfolio_id, owner, risk=RiskPolicy(max_weight_per_ticker=0.05))
    with pytest.raises(ManualOrderRefused, match="allow 5 of the 10") as info:
        place_manual_order(state, lake, _order(portfolio_id, owner), book, tick, now=NOW)
    assert info.value.adjustments and info.value.adjustments[0]["rule"]
    out = place_manual_order(
        state, lake, _order(portfolio_id, owner, allow_reduce=True), book, tick, now=NOW
    )
    assert out.status == "filled" and out.quantity == pytest.approx(5.0)
    assert out.requested_quantity == 10.0


def test_risk_drop_refuses(state, lake, tick, portfolio_id, owner):
    book = _book(portfolio_id, owner, risk=RiskPolicy(max_open_positions=0))
    with pytest.raises(ManualOrderRefused, match="risk rules refuse"):
        place_manual_order(state, lake, _order(portfolio_id, owner), book, tick, now=NOW)


def test_sell_beyond_holding_is_clipped_by_risk(state, lake, tick, portfolio_id, owner):
    with pytest.raises(ManualOrderRefused):
        place_manual_order(
            state,
            lake,
            _order(portfolio_id, owner, side="sell"),
            _book(portfolio_id, owner),
            tick,
            now=NOW,
        )


def test_unpriced_ticker_is_refused(state, lake, tick, portfolio_id, owner):
    with pytest.raises(ManualOrderRefused, match="no recent close"):
        place_manual_order(
            state,
            lake,
            _order(portfolio_id, owner, ticker="NOPE.US"),
            _book(portfolio_id, owner),
            tick,
            now=NOW,
        )


def test_limit_not_marketable_is_recorded_rejected(state, lake, tick, portfolio_id, owner):
    out = place_manual_order(
        state,
        lake,
        _order(portfolio_id, owner, order_type="limit", limit_price=90.0),
        _book(portfolio_id, owner),
        tick,
        now=NOW,
    )
    assert out.status == "rejected" and "not marketable" in (out.reason or "")
    assert state.sql("SELECT COUNT(*) AS n FROM fills")[0]["n"] == 0
    ok = place_manual_order(
        state,
        lake,
        _order(portfolio_id, owner, order_type="limit", limit_price=101.0),
        _book(portfolio_id, owner),
        tick,
        now=NOW,
    )
    assert ok.status == "filled" and ok.fill_price == pytest.approx(100.0)


def test_running_tick_refuses(state, lake, tick, portfolio_id, owner):
    state.execute(
        "INSERT INTO tick_runs (id, started_at, status) VALUES ('t1', ?, 'running')",
        [NOW.isoformat()],
    )
    with pytest.raises(ManualOrderRefused, match="tick is running"):
        place_manual_order(
            state, lake, _order(portfolio_id, owner), _book(portfolio_id, owner), tick, now=NOW
        )


def test_limit_without_price_is_refused(state, lake, tick, portfolio_id, owner):
    with pytest.raises((ManualOrderRefused, ValueError)):
        place_manual_order(
            state,
            lake,
            _order(portfolio_id, owner, order_type="limit"),
            _book(portfolio_id, owner),
            tick,
            now=NOW,
        )


# ---- a book at a broker ---------------------------------------------------------------


class WorkingBroker:
    """Accepts orders and keeps them working until cancelled or filled."""

    def __init__(self, cash: float = 50_000.0, reject: bool = False) -> None:
        self.portfolio = Portfolio(cash=cash)
        self.orders: dict[str, BrokerOrderState] = {}
        self.reject = reject

    def fetch_portfolio(self) -> Portfolio:
        return self.portfolio

    def place_order(self, order: Order):
        from stonks.execution.brokers.base import OrderRejectedError

        if self.reject:
            raise OrderRejectedError("account blocked")
        self.orders[order.client_id] = BrokerOrderState(
            client_id=order.client_id,
            broker_order_id=f"b-{len(self.orders)}",
            ticker=order.ticker,
            side=order.side,
            status="pending",
            quantity=order.quantity,
            filled_quantity=0.0,
            avg_fill_price=None,
        )
        return

    def reconcile(self):
        return []

    def get_order_state(self, client_id: str):
        return self.orders.get(client_id)

    def cancel_order(self, client_id: str) -> bool:
        st = self.orders.get(client_id)
        if st is None or st.status != "pending":
            return False
        from dataclasses import replace

        self.orders[client_id] = replace(st, status="cancelled")
        return True


def test_broker_book_places_pending_then_cancel(state, lake, tick, portfolio_id, owner):
    broker = WorkingBroker()
    book = _book(portfolio_id, owner, broker=broker)
    out = place_manual_order(
        state,
        lake,
        _order(portfolio_id, owner, order_type="limit", limit_price=95.0),
        book,
        tick,
        now=NOW,
    )
    assert out.status == "pending"
    row = state.sql("SELECT * FROM orders WHERE client_id = ?", [out.client_id])[0]
    assert row["broker_order_id"] == "b-0" and row["origin"] == "manual"
    done = cancel_order(
        state, portfolio_id, out.client_id, broker=broker, actor=f"user:{owner}", reason="changed"
    )
    assert done.cancelled and done.status == "cancelled"
    with pytest.raises(ManualOrderRefused, match="only a working order"):
        cancel_order(
            state, portfolio_id, out.client_id, broker=broker, actor="user:x", reason="again"
        )


def test_broker_rejection_is_recorded(state, lake, tick, portfolio_id, owner):
    book = _book(portfolio_id, owner, broker=WorkingBroker(reject=True))
    out = place_manual_order(state, lake, _order(portfolio_id, owner), book, tick, now=NOW)
    assert out.status == "rejected" and out.reason == "account blocked"


def test_change_cancels_and_replaces(state, lake, tick, portfolio_id, owner):
    broker = WorkingBroker()
    book = _book(portfolio_id, owner, broker=broker)
    first = place_manual_order(
        state,
        lake,
        _order(portfolio_id, owner, order_type="limit", limit_price=95.0, client_key="c"),
        book,
        tick,
        now=NOW,
    )
    changed = change_manual_order(
        state,
        lake,
        first.client_id,
        book,
        tick,
        actor=f"user:{owner}",
        reason="lower",
        quantity=6.0,
        limit_price=94.0,
        now=NOW,
    )
    assert changed.client_id == f"{first.client_id}.r1" and changed.status == "pending"
    new = state.sql("SELECT * FROM orders WHERE client_id = ?", [changed.client_id])[0]
    assert new["replaces_client_id"] == first.client_id
    assert new["quantity"] == 6.0 and new["limit_price"] == 94.0
    old = state.sql("SELECT status FROM orders WHERE client_id = ?", [first.client_id])[0]
    assert old["status"] == "cancelled"
    again = change_manual_order(
        state,
        lake,
        changed.client_id,
        book,
        tick,
        actor="user:x",
        reason="r",
        quantity=5.0,
        now=NOW,
    )
    assert again.client_id == f"{first.client_id}.r2"


def test_change_refuses_strategy_orders_and_unknown_ids(state, lake, tick, portfolio_id, owner):
    book = _book(portfolio_id, owner, broker=WorkingBroker())
    with pytest.raises(ManualOrderNotFound):
        change_manual_order(
            state, lake, "nope", book, tick, actor="user:x", reason="r", quantity=1.0
        )
    state.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status, created_at,"
        " updated_at, portfolio_id) VALUES ('s1', 'UP.US', 'buy', 1, 'market', 'pending', ?, ?, ?)",
        [NOW.isoformat(), NOW.isoformat(), portfolio_id],
    )
    with pytest.raises(ManualOrderRefused, match="only a manual order"):
        change_manual_order(state, lake, "s1", book, tick, actor="user:x", reason="r", quantity=2)


def test_simulated_book_has_nothing_to_cancel(state, lake, tick, portfolio_id, owner):
    out = place_manual_order(
        state, lake, _order(portfolio_id, owner), _book(portfolio_id, owner), tick, now=NOW
    )
    with pytest.raises(ManualOrderRefused):
        cancel_order(state, portfolio_id, out.client_id, broker=None, actor="user:x", reason="r")


def test_other_portfolio_order_is_not_found(state, lake, tick, portfolio_id, owner):
    out = place_manual_order(
        state, lake, _order(portfolio_id, owner), _book(portfolio_id, owner), tick, now=NOW
    )
    with pytest.raises(ManualOrderNotFound):
        cancel_order(state, "pf_other", out.client_id, broker=None, actor="user:x", reason="r")


def test_snapshot_date_is_the_order_day(state, lake, tick, portfolio_id, owner):
    place_manual_order(
        state, lake, _order(portfolio_id, owner), _book(portfolio_id, owner), tick, now=NOW
    )
    snap = state.sql("SELECT as_of, tick_id FROM portfolio_snapshots")[0]
    assert snap["as_of"] == date(2026, 4, 2).isoformat() and snap["tick_id"] is None
