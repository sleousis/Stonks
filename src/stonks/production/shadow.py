"""Model books (roadmap 2.4, BL-12): evaluate strategies without trading.

Each ``shadow`` strategy (and, with ``TickSettings.model_books = "all"``,
each ``active`` one too) runs as if it were the sole active strategy, against
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

The strategy that decides is the instance the tick's signal phase scored
with (``strategies``), so per-day state from ``estimate_return`` reaches
``decide``; without it, each strategy is loaded from the registry.

Like the real tick, a strategy with no ranked picks but open virtual
positions still decides (with no picks) so its exits happen; one with no
picks and a flat portfolio holds, and is only marked to market. Buys need
a fresh close (``buyable``); holdings are marked and sold at their last
known close.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Literal

from stonks.core.corporate_actions import CorporateActions
from stonks.core.protocols import Strategy
from stonks.core.types import Fill, Order, Portfolio
from stonks.execution.orders import make_client_id
from stonks.logging import get_logger
from stonks.production.corporate_actions import apply_corporate_actions
from stonks.production.prices import drop_stale_buys, held_tickers
from stonks.production.risk import RiskContext, apply_risk, model_book_risk_context
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
    buyable: Collection[str] | None = None,
    volumes: Mapping[str, float] | None = None,
    corporate_actions: CorporateActions | None = None,
    strategies: Callable[[str], Strategy] | None = None,
    statuses: Sequence[str] = ("shadow",),
    risk_context: RiskContext | None = None,
) -> list[ShadowOutcome]:
    """``buyable`` restricts buys to tickers with a fresh close (default:
    any ticker in ``prices``). ``volumes`` (of each priced bar) feed the
    cost model's impact term, as in the real tick. ``corporate_actions``
    due since a virtual portfolio's snapshot are applied to it before it
    decides, exactly as for the real portfolio (and persisted with its
    new snapshot, so once). ``strategies(sid)`` returns the instance that
    decides (default: loaded from the registry); ``statuses`` are the
    registry statuses that keep a model book. ``risk_context`` (history and
    sectors, from ``build_risk_context``) lets the rules that need history
    run; each model book gets its own equity curve and entry dates."""
    fresh = set(prices) if buyable is None else set(buyable)
    picks_by_strategy: dict[str, list[tuple[float, str]]] = {}
    for r, sid, ticker in ranked:
        picks_by_strategy.setdefault(sid, []).append((r, ticker))

    outcomes: list[ShadowOutcome] = []
    handles = [h for status in statuses for h in registry.list_all(status=status)]
    for handle in handles:
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
                fresh,
                volumes or {},
                corporate_actions or CorporateActions(),
                strategies or registry.load,
                risk_context,
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
    fresh: Collection[str],
    volumes: Mapping[str, float],
    corporate_actions: CorporateActions,
    strategies: Callable[[str], Strategy],
    risk_context: RiskContext | None = None,
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

    portfolio, since = _load_virtual_portfolio(state, strategy_id, settings.initial_cash)
    apply_corporate_actions(
        portfolio,
        corporate_actions,
        since=since,
        as_of=as_of,
        withholding_rate=settings.dividend_withholding_rate,
    )
    # Load even without picks: the Ranker silently skips a strategy that
    # fails to load, and that must surface as ``failed``, not ``evaluated``.
    strategy = strategies(strategy_id)

    orders: list[Order] = []
    if picks or held_tickers(portfolio.positions):
        proposed, _ = drop_stale_buys(strategy.decide(picks, portfolio, prices, as_of), fresh)
        risk_result = apply_risk(
            proposed,
            portfolio,
            prices,
            asset_classes,
            settings.risk,
            slippage_bps=settings.slippage_bps,
            fee_per_trade=settings.fee_per_trade,
            cost_model=settings.costs,
            volumes=volumes,
            context=(
                model_book_risk_context(risk_context, state, strategy_id, portfolio, as_of)
                if risk_context is not None
                else None
            ),
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
            for o in _one_per_side(risk_result.orders)
        ]

    # Always an in-memory simulated broker: shadow must never reach a real
    # one. Same costs, asset classes and volumes as the real simulated tick.
    broker = settings.simulated_costs.build_broker(portfolio)
    broker.set_asset_classes(asset_classes)  # type: ignore[arg-type]
    broker.set_prices(prices, as_of=as_of, volumes=volumes)
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


def _one_per_side(orders: Sequence[Order]) -> list[Order]:
    """One order per (ticker, side), quantities summed: a model book keys
    its decisions by strategy, day, ticker and side, so a rule's top-up
    (e.g. a max_holding forced sell next to the strategy's own partial
    sell) must join the strategy's order, not collide with it."""
    merged: dict[tuple[str, str], Order] = {}
    for o in orders:
        key = (o.ticker, o.side)
        prev = merged.get(key)
        merged[key] = o if prev is None else replace(prev, quantity=prev.quantity + o.quantity)
    return list(merged.values())


def _load_virtual_portfolio(
    state: SqliteState, strategy_id: str, initial_cash: float
) -> tuple[Portfolio, date | None]:
    """The latest virtual portfolio and its snapshot's ``as_of`` (``None``
    when freshly seeded)."""
    rows = state.sql(
        "SELECT as_of, cash, positions_json FROM shadow_portfolio_snapshots"
        " WHERE strategy_id = ? ORDER BY as_of DESC, id DESC LIMIT 1",
        [strategy_id],
    )
    if not rows:
        return Portfolio(cash=initial_cash, positions={}), None
    row = rows[0]
    portfolio = Portfolio(cash=float(row["cash"]), positions=json.loads(row["positions_json"]))
    return portfolio, date.fromisoformat(row["as_of"]) if row["as_of"] else None


def shadow_held_tickers(state: SqliteState) -> list[str]:
    """Every ticker held in any strategy's latest virtual portfolio."""
    latest: dict[str, str] = {}
    for row in state.sql(
        "SELECT strategy_id, positions_json FROM shadow_portfolio_snapshots ORDER BY as_of, id"
    ):
        latest[row["strategy_id"]] = row["positions_json"]
    held: set[str] = set()
    for positions_json in latest.values():
        held.update(held_tickers(json.loads(positions_json)))
    return sorted(held)


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
