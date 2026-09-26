"""Shadow mode (roadmap 2.4): evaluate ``shadow`` strategies without trading.

Each shadow strategy runs as if it were the sole active strategy, against
its own virtual portfolio persisted in ``shadow_portfolio_snapshots`` (one
row per strategy per ``as_of``), seeded from ``initial_cash``. Its proposed
orders pass through the same risk policy, are "filled" on an in-memory
``SimulatedBroker`` (always simulated, whatever broker the real tick uses),
and are logged to ``shadow_decisions`` as promotion evidence.

Isolation guarantees:

- nothing here reads or writes ``orders`` / ``fills`` / ``portfolio_snapshots``;
- each strategy's decisions + snapshot commit in one transaction, and any
  exception is caught per strategy and reported as a ``failed`` outcome;
- a strategy already evaluated for ``as_of`` (or for a later date) is skipped,
  so re-running a tick never double-applies virtual fills.

Like the real tick, a strategy with no ranked picks today holds: its
portfolio is only marked to market.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Literal

from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Fill, Order, Portfolio
from stonks.execution.orders import make_client_id
from stonks.logging import get_logger
from stonks.production.risk import apply_risk
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState

if TYPE_CHECKING:
    from stonks.production.tick import TickSettings

ShadowStatus = Literal["evaluated", "already_evaluated", "out_of_order", "failed"]

_log = get_logger("stonks.production.shadow")


@dataclass(frozen=True)
class ShadowOutcome:
    strategy_id: str
    status: ShadowStatus
    decisions: int = 0
    fills: int = 0
    total_value: float | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "status": self.status,
            "decisions": self.decisions,
            "fills": self.fills,
            "total_value": self.total_value,
            "error": self.error,
        }


def evaluate_shadow_strategies(
    state: SqliteState,
    registry: StrategyRegistry,
    ranked: Sequence[tuple[float, str, str]],
    prices: Mapping[str, float],
    asset_classes: Mapping[str, str],
    as_of: date,
    tick_id: str,
    settings: TickSettings,
) -> list[ShadowOutcome]:
    picks_by_strategy: dict[str, list[tuple[float, str]]] = {}
    for r, sid, ticker in ranked:
        picks_by_strategy.setdefault(sid, []).append((r, ticker))

    outcomes: list[ShadowOutcome] = []
    for handle in registry.list_all(status="shadow"):
        log = _log.bind(tick_id=tick_id, strategy_id=handle.id, as_of=as_of.isoformat())
        try:
            outcome = _evaluate_one(
                state,
                registry,
                handle.id,
                picks_by_strategy.get(handle.id, []),
                prices,
                asset_classes,
                as_of,
                tick_id,
                settings,
            )
        except Exception as exc:
            log.warning("shadow.failed", error=str(exc), error_type=type(exc).__name__)
            outcome = ShadowOutcome(
                strategy_id=handle.id, status="failed", error=f"{type(exc).__name__}: {exc}"
            )
        else:
            log.info("shadow.outcome", **outcome.as_dict())
        outcomes.append(outcome)
    return outcomes


def _evaluate_one(
    state: SqliteState,
    registry: StrategyRegistry,
    strategy_id: str,
    picks: list[tuple[float, str]],
    prices: Mapping[str, float],
    asset_classes: Mapping[str, str],
    as_of: date,
    tick_id: str,
    settings: TickSettings,
) -> ShadowOutcome:
    latest = state.sql(
        "SELECT as_of FROM shadow_portfolio_snapshots WHERE strategy_id = ? AND as_of >= ? "
        "ORDER BY as_of DESC LIMIT 1",
        [strategy_id, as_of.isoformat()],
    )
    if latest:
        status: ShadowStatus = (
            "already_evaluated" if latest[0]["as_of"] == as_of.isoformat() else "out_of_order"
        )
        return ShadowOutcome(strategy_id=strategy_id, status=status)

    portfolio = _load_virtual_portfolio(state, strategy_id, settings.initial_cash)

    orders: list[Order] = []
    if picks:
        strategy = registry.load(strategy_id)
        proposed = strategy.decide(picks, portfolio, prices, as_of)
        risk_result = apply_risk(
            proposed,
            portfolio,
            prices,
            asset_classes,
            settings.risk,
            slippage_bps=settings.slippage_bps,
            fee_per_trade=settings.fee_per_trade,
        )
        orders = [
            replace(
                o,
                tick_id=tick_id,
                strategy_id=strategy_id,
                client_id=make_client_id(
                    as_of=as_of, strategy_id=strategy_id, ticker=o.ticker, side=o.side
                ),
            )
            for o in risk_result.orders
        ]

    # Always an in-memory simulated broker: shadow must never reach a real one.
    broker = SimulatedBroker(
        portfolio=portfolio,
        slippage_bps=settings.slippage_bps,
        fee_per_trade=settings.fee_per_trade,
    )
    broker.set_prices(prices, as_of=as_of)
    results: list[tuple[Order, Fill | None]] = [(o, broker.place_order(o)) for o in orders]

    total_value = portfolio.total_value(prices)
    now = _iso_now()
    with state.transaction():
        for order, fill in results:
            state.execute(
                """
                INSERT INTO shadow_decisions
                    (tick_id, strategy_id, as_of, ticker, side, quantity, price, status,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (strategy_id, as_of, ticker, side) DO NOTHING
                """,
                [
                    tick_id,
                    strategy_id,
                    as_of.isoformat(),
                    order.ticker,
                    order.side,
                    fill.quantity if fill else order.quantity,
                    fill.price if fill else prices.get(order.ticker),
                    "filled" if fill else "rejected",
                    now,
                ],
            )
        state.execute(
            """
            INSERT INTO shadow_portfolio_snapshots
                (tick_id, strategy_id, as_of, taken_at, cash, positions_json, total_value)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                tick_id,
                strategy_id,
                as_of.isoformat(),
                now,
                portfolio.cash,
                json.dumps(portfolio.positions, sort_keys=True),
                total_value,
            ],
        )

    return ShadowOutcome(
        strategy_id=strategy_id,
        status="evaluated",
        decisions=len(results),
        fills=sum(1 for _, f in results if f is not None),
        total_value=total_value,
    )


def _load_virtual_portfolio(state: SqliteState, strategy_id: str, initial_cash: float) -> Portfolio:
    rows = state.sql(
        "SELECT cash, positions_json FROM shadow_portfolio_snapshots WHERE strategy_id = ? "
        "ORDER BY as_of DESC, id DESC LIMIT 1",
        [strategy_id],
    )
    if not rows:
        return Portfolio(cash=initial_cash, positions={})
    return Portfolio(cash=float(rows[0]["cash"]), positions=json.loads(rows[0]["positions_json"]))


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
