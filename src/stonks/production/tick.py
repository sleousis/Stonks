"""Production tick — one-shot entrypoint invoked by an external scheduler.

Each invocation is a fresh process. State lives in SqliteState + DuckDBLake;
the tick is stateless across runs. Orders are idempotent via a deterministic
``client_id`` derived from (tick_id, strategy_id, ticker, side), so a crashed
tick re-run on the same day will short-circuit duplicate submissions at the
broker.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Order, Portfolio
from stonks.execution.orders import make_client_id
from stonks.logging import get_logger
from stonks.production.ranker import Ranker
from stonks.registry.store import StrategyRegistry
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

_log = get_logger("stonks.production.tick")


@dataclass(frozen=True)
class TickSettings:
    universe: Sequence[str]
    threshold: float = 0.0
    initial_cash: float = 10_000.0
    slippage_bps: float = 0.0
    fee_per_trade: float = 0.0


@dataclass(frozen=True)
class TickResult:
    tick_id: str
    status: str  # 'ok' | 'partial' | 'error' | 'noop'
    winner_strategy_id: str | None
    orders_placed: int
    fills: int


def run_tick(
    state: SqliteState,
    lake: DuckDBLake,
    registry: StrategyRegistry,
    settings: TickSettings,
    as_of: date | None = None,
    dry_run: bool = False,
) -> TickResult:
    as_of = as_of or date.today()
    tick_id = _new_tick_id(as_of)
    started = _iso_now()
    log = _log.bind(tick_id=tick_id, as_of=as_of.isoformat(), dry_run=dry_run)

    state.execute(
        "INSERT INTO tick_runs (id, started_at, status) VALUES (?, ?, 'running')",
        [tick_id, started],
    )

    # 1. rank
    ranker = Ranker(
        registry=registry,
        lake=lake,
        universe=settings.universe,
        threshold=settings.threshold,
    )
    ranked = ranker.rank(as_of=as_of)

    if not ranked:
        _close_tick(state, tick_id, status="noop", summary={"reason": "no_candidates"})
        log.info("tick.noop", reason="no_candidates")
        return TickResult(
            tick_id=tick_id, status="noop", winner_strategy_id=None,
            orders_placed=0, fills=0,
        )

    winner_return, winner_id, winner_ticker = ranked[0]
    log.info(
        "tick.winner",
        strategy_id=winner_id,
        ticker=winner_ticker,
        expected_return=winner_return,
    )
    strategy = registry.load(winner_id)
    my_picks = [(r, t) for r, sid, t in ranked if sid == winner_id]

    # 2. prepare portfolio + prices
    portfolio = _load_or_seed_portfolio(state, initial_cash=settings.initial_cash)
    prices = _current_prices(lake, settings.universe, as_of)

    broker = SimulatedBroker(
        portfolio=portfolio,
        slippage_bps=settings.slippage_bps,
        fee_per_trade=settings.fee_per_trade,
    )
    broker.set_prices(prices, as_of=as_of)

    # 3. decide + place
    orders = strategy.decide(my_picks, portfolio, prices, as_of)
    orders_with_tick = [
        replace(
            o,
            tick_id=tick_id,
            strategy_id=winner_id,
            client_id=make_client_id(
                tick_id=tick_id, strategy_id=winner_id, ticker=o.ticker, side=o.side
            ),
        )
        for o in orders
    ]

    placed = 0
    fills_count = 0
    any_failure = False
    for order in orders_with_tick:
        if dry_run:
            placed += 1
            continue
        try:
            fill = broker.place_order(order)
        except Exception as exc:
            any_failure = True
            log.warning("tick.order.failed", ticker=order.ticker, error=str(exc))
            continue

        _record_order(state, order, status="filled" if fill else "rejected")
        placed += 1
        if fill is not None:
            _record_fill(state, fill)
            fills_count += 1

    if not dry_run:
        _snapshot_portfolio(state, tick_id, portfolio, prices, as_of)

    status = "ok" if not any_failure else "partial"
    _close_tick(
        state,
        tick_id,
        status=status,
        summary={
            "winner_strategy_id": winner_id,
            "winner_expected_return": winner_return,
            "orders_placed": placed,
            "fills": fills_count,
        },
    )
    return TickResult(
        tick_id=tick_id,
        status=status,
        winner_strategy_id=winner_id,
        orders_placed=placed,
        fills=fills_count,
    )


# ---- helpers ---------------------------------------------------------------


def _new_tick_id(as_of: date) -> str:
    return f"tick_{as_of.isoformat()}_{uuid.uuid4().hex[:8]}"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _load_or_seed_portfolio(state: SqliteState, initial_cash: float) -> Portfolio:
    rows = state.sql(
        "SELECT cash, positions_json FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
    )
    if not rows:
        return Portfolio(cash=initial_cash, positions={})
    row = rows[0]
    return Portfolio(cash=float(row["cash"]), positions=json.loads(row["positions_json"]))


def _current_prices(lake: DuckDBLake, universe: Sequence[str], as_of: date) -> dict[str, float]:
    prices: dict[str, float] = {}
    for ticker in universe:
        df = lake.sql(
            "SELECT close FROM prices WHERE ticker=? AND date<=? "
            "ORDER BY date DESC LIMIT 1",
            [ticker, as_of],
        )
        if not df.empty:
            prices[ticker] = float(df.iloc[0]["close"])
    return prices


def _record_order(state: SqliteState, order: Order, status: str) -> None:
    # Use INSERT OR IGNORE: if a previous (crashed) tick already inserted this
    # client_id, we leave that row alone — the broker won't double-fill either.
    now = _iso_now()
    state.execute(
        """
        INSERT OR IGNORE INTO orders
            (client_id, tick_id, strategy_id, ticker, side, quantity,
             order_type, limit_price, status, broker_order_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        [
            order.client_id,
            order.tick_id,
            order.strategy_id,
            order.ticker,
            order.side,
            order.quantity,
            order.order_type,
            order.limit_price,
            status,
            now,
            now,
        ],
    )


def _record_fill(state: SqliteState, fill) -> None:  # type: ignore[no-untyped-def]
    state.execute(
        """
        INSERT INTO fills
            (order_client_id, ticker, quantity, price, fee, filled_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            fill.order_client_id,
            fill.ticker,
            fill.quantity,
            fill.price,
            fill.fee,
            fill.filled_at.isoformat(timespec="seconds"),
        ],
    )


def _snapshot_portfolio(
    state: SqliteState,
    tick_id: str,
    portfolio: Portfolio,
    prices: dict[str, float],
    as_of: date,
) -> None:
    total = portfolio.total_value(prices)
    state.execute(
        """
        INSERT INTO portfolio_snapshots
            (tick_id, taken_at, cash, positions_json, total_value)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            tick_id,
            _iso_now(),
            portfolio.cash,
            json.dumps(portfolio.positions, sort_keys=True),
            total,
        ],
    )


def _close_tick(state: SqliteState, tick_id: str, status: str, summary: dict) -> None:
    # The tick_runs CHECK constraint accepts 'ok' | 'partial' | 'error' | 'running'.
    # 'noop' is a TickResult-only distinction (no candidates were ranked); persist
    # it as 'ok' and leave the "it was a no-op" signal in summary_json.
    db_status = "ok" if status == "noop" else status
    state.execute(
        "UPDATE tick_runs SET finished_at=?, status=?, summary_json=? WHERE id=?",
        [_iso_now(), db_status, json.dumps(summary, sort_keys=True), tick_id],
    )
