"""Production tick — one-shot entrypoint invoked by an external scheduler.

Each invocation is a fresh process. State lives in SqliteState + DuckDBLake;
the tick is stateless across runs. Orders are idempotent via a deterministic
``client_id`` derived from (as_of date, strategy_id, ticker, side) — not from
the per-run ``tick_id``, which stays unique so every run gets its own
``tick_runs`` row. Re-running a crashed tick for the same ``as_of`` therefore
reproduces the same client_ids, and any order already in ``orders`` (unless
``rejected`` / ``cancelled``) is skipped instead of being submitted (and
applied to the portfolio) again.

The broker is the simulated one unless ``TickSettings.broker_kind`` opts in
to an external broker (``alpaca``, built by ``broker_factory``). Then the tick
reconciles open orders before deciding, takes the portfolio from the broker
account, records submitted orders as ``pending`` and books fills only through
``execution.reconcile.reconcile_orders``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any, Literal

from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.protocols import Broker
from stonks.core.types import Fill, Order, OrderStatus, Portfolio
from stonks.execution.brokers.base import BrokerKind, OrderRejectedError
from stonks.execution.orders import make_client_id
from stonks.execution.reconcile import NON_TERMINAL_STATUSES, reconcile_orders
from stonks.logging import get_logger
from stonks.notify import Notification, Notifier
from stonks.production.prices import drop_stale_buys, held_tickers, load_prices
from stonks.production.ranker import Ranker
from stonks.production.risk import RiskPolicy, apply_risk
from stonks.production.shadow import evaluate_shadow_strategies, shadow_held_tickers
from stonks.registry.store import StrategyRegistry
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

TickStatus = Literal["ok", "partial", "error", "noop"]
_DBTickStatus = Literal["running", "ok", "partial", "error"]

_log = get_logger("stonks.production.tick")

#: Builds the tick's broker around the portfolio it trades (a simulated
#: broker trades that object in memory; an external one ignores it).
BrokerFactory = Callable[[Portfolio], Broker]


class BackdatedTickError(ValueError):
    """A non-dry-run tick was asked to trade a date earlier than the latest
    portfolio snapshot. Trading it would apply today's portfolio to an old
    date and write a new "latest" snapshot that belongs in the past."""


@dataclass(frozen=True)
class TickSettings:
    universe: Sequence[str]
    threshold: float = 0.0
    initial_cash: float = 10_000.0
    slippage_bps: float = 0.0
    fee_per_trade: float = 0.0
    # Closes older than this many calendar days before ``as_of`` are ignored,
    # so delisted / failed-ingest tickers never fill at months-old prices.
    max_price_staleness_days: int = 7
    risk: RiskPolicy = field(default_factory=RiskPolicy)
    # Evaluate shadow strategies against virtual portfolios (never traded).
    shadow_enabled: bool = True
    # "simulated" (default) trades in memory against the snapshot portfolio.
    # "alpaca" trades through ``run_tick``'s ``broker_factory``; the broker
    # account is then the source of truth for the portfolio.
    broker_kind: BrokerKind = "simulated"


@dataclass(frozen=True)
class TickResult:
    tick_id: str
    status: TickStatus
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
    notifier: Notifier | None = None,
    broker_factory: BrokerFactory | None = None,
) -> TickResult:
    as_of = as_of or utc_today()
    tick_id = _new_tick_id(as_of)
    started = _iso_now()
    log = _log.bind(tick_id=tick_id, as_of=as_of.isoformat(), dry_run=dry_run)
    if not dry_run:
        _refuse_backdated(state, as_of, log)

    state.execute(
        "INSERT INTO tick_runs (id, started_at, status) VALUES (?, ?, 'running')",
        [tick_id, started],
    )

    # Any failure past this point closes the tick as 'error' so the ledger
    # never keeps a row stuck at 'running'; the exception still propagates.
    try:
        result = _run_tick_body(
            state,
            lake,
            registry,
            settings,
            as_of,
            dry_run,
            tick_id=tick_id,
            log=log,
            notifier=notifier,
            broker_factory=broker_factory,
        )
    except Exception as exc:
        log.error("tick.error", error=str(exc), error_type=type(exc).__name__)
        try:
            _close_tick(
                state,
                tick_id,
                status="error",
                summary={"error": str(exc), "error_type": type(exc).__name__},
            )
        except Exception as close_exc:  # pragma: no cover - best effort
            log.error("tick.close_failed", error=str(close_exc))
        _safe_notify(
            notifier,
            Notification(
                level="error",
                title="tick failed",
                message=f"{type(exc).__name__}: {exc}",
                fields={"tick_id": tick_id, "as_of": as_of.isoformat(), "status": "error"},
            ),
            log,
        )
        raise
    if result.status == "partial":
        _safe_notify(
            notifier,
            Notification(
                level="warning",
                title="tick partially failed",
                message="one or more orders raised at the broker; see logs",
                fields={
                    "tick_id": tick_id,
                    "as_of": as_of.isoformat(),
                    "status": result.status,
                },
            ),
            log,
        )
    return result


def _run_tick_body(
    state: SqliteState,
    lake: DuckDBLake,
    registry: StrategyRegistry,
    settings: TickSettings,
    as_of: date,
    dry_run: bool,
    *,
    tick_id: str,
    log: Any,
    notifier: Notifier | None,
    broker_factory: BrokerFactory | None,
) -> TickResult:
    # 1. rank active strategies.
    ranker = Ranker(
        registry=registry,
        lake=lake,
        universe=settings.universe,
        threshold=settings.threshold,
    )
    ranked = ranker.rank(as_of=as_of)

    # 2. prepare the portfolio, then price the universe plus every holding:
    #    a held ticker outside the universe must still be marked and sellable.
    #    An external broker (opt-in, e.g. Alpaca) is the source of truth: sync
    #    its order statuses and fills into the ledger first, then read the
    #    portfolio from the account.
    external = settings.broker_kind != "simulated"
    broker: Broker | None = None
    if external:
        if broker_factory is None:
            raise ValueError(
                f"broker_kind={settings.broker_kind!r} needs a broker_factory "
                "(build it with production.settings_builder.build_tick_runtime)"
            )
        broker = broker_factory(Portfolio(cash=0.0))
        if not dry_run:
            pre = reconcile_orders(broker, state)
            log.info(
                "tick.reconciled",
                orders_checked=pre.orders_checked,
                fills_inserted=pre.fills_inserted,
            )
        portfolio = broker.fetch_portfolio()
    else:
        portfolio = _load_or_seed_portfolio(state, initial_cash=settings.initial_cash)
    held = held_tickers(portfolio.positions)
    book = load_prices(
        lake,
        settings.universe,
        held,
        as_of,
        max_staleness_days=settings.max_price_staleness_days,
    )
    prices = book.prices

    def noop(reason: str) -> TickResult:
        summary: dict[str, Any] = {"reason": reason}
        summary.update(_shadow_phase(state, lake, registry, settings, as_of, dry_run, tick_id, log))
        _close_tick(state, tick_id, status="noop", summary=summary)
        log.info("tick.noop", reason=reason)
        return TickResult(
            tick_id=tick_id,
            status="noop",
            winner_strategy_id=None,
            orders_placed=0,
            fills=0,
        )

    winner_return: float | None
    exit_strategy_id: str | None = None
    if ranked:
        winner_return, winner_id, winner_ticker = ranked[0]
        log.info(
            "tick.winner",
            strategy_id=winner_id,
            ticker=winner_ticker,
            expected_return=winner_return,
        )
        my_picks = [(r, t) for r, sid, t in ranked if sid == winner_id]
    else:
        # Nothing ranked. A flat book has nothing to do; otherwise the
        # strategy that owns the positions still decides (with no picks),
        # so exits (momentum drop-outs, regime risk-off, ...) happen.
        if not held:
            return noop("no_candidates")
        owner = _position_owner(state, registry, held, log)
        if owner is None:
            return noop("no_active_owner")
        log.info("tick.exit_decision", strategy_id=owner, held=held)
        winner_return, winner_id, exit_strategy_id, my_picks = None, owner, owner, []
    strategy = registry.load(winner_id)

    # 3. decide, drop buys priced off stale closes, then let the risk layer
    #    clip/drop before anything reaches the broker.
    proposed, stale_buys = drop_stale_buys(
        strategy.decide(my_picks, portfolio, prices, as_of), book.fresh
    )
    asset_classes = _asset_classes(lake, [*settings.universe, *portfolio.positions])
    risk_result = apply_risk(
        proposed,
        portfolio,
        prices,
        asset_classes,
        settings.risk,
        slippage_bps=settings.slippage_bps,
        fee_per_trade=settings.fee_per_trade,
    )
    risk_adjustments = list(risk_result.adjustments)

    if broker is None:
        broker = _build_broker(portfolio, settings, prices, as_of, broker_factory)
    orders_with_tick = [
        replace(
            o,
            tick_id=tick_id,
            strategy_id=winner_id,
            client_id=make_client_id(
                as_of=as_of, strategy_id=winner_id, ticker=o.ticker, side=o.side
            ),
        )
        for o in risk_result.orders
    ]
    open_conflicts: list[dict[str, str]] = []
    if external:
        orders_with_tick, open_conflicts = _drop_open_order_conflicts(state, orders_with_tick, log)
    sells = [o for o in orders_with_tick if o.side == "sell"]
    buys = [o for o in orders_with_tick if o.side == "buy"]

    placed = 0
    fills_count = 0
    any_failure = False
    # Simulated: broker outcomes are buffered and persisted together with the
    # portfolio snapshot in one transaction, so a crash can't leave fills
    # recorded without the snapshot that reflects them. External: each order
    # row is written the moment the broker has it (the order exists there
    # whatever happens next), always as 'pending'; statuses and fills are
    # then booked only by ``reconcile_orders``, from the broker's cumulative
    # state, so no fill is ever booked twice.
    outcomes: list[tuple[Order, OrderStatus, Fill | None]] = []

    def place(batch: list[Order]) -> None:
        nonlocal placed, fills_count, any_failure
        for order in batch:
            if dry_run:
                placed += 1
                continue
            existing = _live_order_status(state, order.client_id)
            if existing is not None:
                log.info(
                    "tick.order.skipped_already_submitted",
                    client_id=order.client_id,
                    status=existing,
                )
                continue
            try:
                fill = broker.place_order(order)
            except OrderRejectedError as exc:
                log.warning("tick.order.rejected", ticker=order.ticker, error=str(exc))
                if external:
                    _record_order(state, order, status="rejected")
                outcomes.append((order, "rejected", None))
                continue
            except Exception as exc:
                any_failure = True
                log.warning("tick.order.failed", ticker=order.ticker, error=str(exc))
                continue
            placed += 1
            if external:
                _record_order(state, order, status="pending")
                outcomes.append((order, "pending", None))
                continue
            outcomes.append((order, "filled" if fill else "rejected", fill))
            if fill is not None:
                fills_count += 1

    # Sells first. The first risk pass counted their expected proceeds, so
    # buys are re-checked against the portfolio as it stands after the
    # sells: a rejected or unfilled sell must not fund a buy. (An external
    # broker's portfolio object is not updated by fills, so its buys are
    # checked against the pre-sell cash: conservative by construction.)
    place(sells)
    if buys and not dry_run:
        second = apply_risk(
            buys,
            portfolio,
            prices,
            asset_classes,
            settings.risk,
            slippage_bps=settings.slippage_bps,
            fee_per_trade=settings.fee_per_trade,
        )
        risk_adjustments.extend(second.adjustments)
        buys = second.orders
    place(buys)

    if not dry_run and external:
        post = reconcile_orders(broker, state)
        fills_count = post.fills_inserted
        # Report what the broker made of each submission (e.g. rejected).
        booked = _order_statuses(state, [o.client_id for o, _, _ in outcomes])
        outcomes = [(o, booked.get(o.client_id, st), f) for o, st, f in outcomes]
        after = broker.fetch_portfolio()
        marks = dict(prices)
        unpriced = [t for t in held_tickers(after.positions) if t not in marks]
        if unpriced:
            marks.update(
                load_prices(
                    lake,
                    [],
                    unpriced,
                    as_of,
                    max_staleness_days=settings.max_price_staleness_days,
                ).prices
            )
        with state.transaction():
            _snapshot_portfolio(state, tick_id, after, marks, as_of)
    elif not dry_run:
        with state.transaction():
            for order, order_status, fill in outcomes:
                _record_order(state, order, status=order_status)
                if fill is not None:
                    _record_fill(state, fill)
            _snapshot_portfolio(state, tick_id, portfolio, prices, as_of)

    rejected = [order.ticker for order, st, _ in outcomes if st == "rejected"]
    if rejected:
        _safe_notify(
            notifier,
            Notification(
                level="warning",
                title="orders rejected",
                message=f"{len(rejected)} order(s) rejected by the broker",
                fields={"tick_id": tick_id, "as_of": as_of.isoformat(), "rejected": rejected},
            ),
            log,
        )

    # 4. shadow strategies, strictly after the real ledger has committed.
    shadow_summary = _shadow_phase(state, lake, registry, settings, as_of, dry_run, tick_id, log)

    status: TickStatus = "ok" if not any_failure else "partial"
    _close_tick(
        state,
        tick_id,
        status=status,
        summary={
            "winner_strategy_id": None if exit_strategy_id else winner_id,
            "winner_expected_return": winner_return,
            **(
                {"reason": "no_candidates", "exit_strategy_id": exit_strategy_id}
                if exit_strategy_id
                else {}
            ),
            "stale_buys_dropped": stale_buys,
            **({"open_order_conflicts": open_conflicts} if open_conflicts else {}),
            "orders_placed": placed,
            "fills": fills_count,
            "risk_adjustments": [a.as_dict() for a in risk_adjustments],
            **shadow_summary,
        },
    )
    return TickResult(
        tick_id=tick_id,
        status=status,
        winner_strategy_id=None if exit_strategy_id else winner_id,
        orders_placed=placed,
        fills=fills_count,
    )


# ---- helpers ---------------------------------------------------------------


def _build_broker(
    portfolio: Portfolio,
    settings: TickSettings,
    prices: dict[str, float],
    as_of: date,
    factory: BrokerFactory | None = None,
) -> Broker:
    """The one place the tick constructs its broker around ``portfolio``."""
    if factory is not None:
        broker = factory(portfolio)
    else:
        broker = SimulatedBroker(
            portfolio=portfolio,
            slippage_bps=settings.slippage_bps,
            fee_per_trade=settings.fee_per_trade,
        )
    if isinstance(broker, SimulatedBroker):
        broker.set_prices(prices, as_of=as_of)
    return broker


def _shadow_phase(
    state: SqliteState,
    lake: DuckDBLake,
    registry: StrategyRegistry,
    settings: TickSettings,
    as_of: date,
    dry_run: bool,
    tick_id: str,
    log: Any,
) -> dict[str, Any]:
    """Rank and evaluate shadow strategies; return the tick-summary fragment.
    Never raises: a shadow failure must not fail the real tick."""
    if dry_run or not settings.shadow_enabled:
        return {}
    try:
        shadow_ranked = Ranker(
            registry=registry,
            lake=lake,
            universe=settings.universe,
            threshold=settings.threshold,
            status="shadow",
        ).rank(as_of=as_of)
        # Shadow portfolios may hold tickers outside the universe too.
        shadow_held = shadow_held_tickers(state)
        book = load_prices(
            lake,
            settings.universe,
            shadow_held,
            as_of,
            max_staleness_days=settings.max_price_staleness_days,
        )
        outcomes = evaluate_shadow_strategies(
            state,
            registry,
            shadow_ranked,
            book.prices,
            _asset_classes(lake, [*settings.universe, *shadow_held]),
            as_of,
            tick_id,
            settings,
            buyable=book.fresh,
        )
    except Exception as exc:
        log.error("tick.shadow_failed", error=str(exc), error_type=type(exc).__name__)
        return {"shadow_error": f"{type(exc).__name__}: {exc}"}
    return {"shadow": [o.as_dict() for o in outcomes]}


def _safe_notify(notifier: Notifier | None, notification: Notification, log: Any) -> None:
    """Notifiers promise never to raise; guard anyway so a misbehaving one
    can never fail or mask the outcome of a tick."""
    if notifier is None:
        return
    try:
        notifier.notify(notification)
    except Exception as exc:
        log.error("tick.notify_failed", error=str(exc), error_type=type(exc).__name__)


def _asset_classes(lake: DuckDBLake, tickers: Sequence[str]) -> dict[str, str]:
    unique = sorted(set(tickers))
    return lake.get_asset_classes(unique) if unique else {}


def utc_today() -> date:
    """Today's date in UTC — the calendar all stored timestamps use."""
    return datetime.now(UTC).date()


def _new_tick_id(as_of: date) -> str:
    return f"tick_{as_of.isoformat()}_{uuid.uuid4().hex[:8]}"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _refuse_backdated(state: SqliteState, as_of: date, log: Any) -> None:
    latest = state.sql("SELECT MAX(as_of) AS as_of FROM portfolio_snapshots")[0]["as_of"]
    if latest is not None and as_of.isoformat() < latest:
        log.error("tick.backdated_refused", latest_snapshot_as_of=latest)
        raise BackdatedTickError(
            f"refusing to trade as_of {as_of.isoformat()}: the portfolio already has a "
            f"snapshot for {latest}; run with --dry-run to inspect a past date"
        )


def _load_or_seed_portfolio(state: SqliteState, initial_cash: float) -> Portfolio:
    # NULL as_of (rows written without one) sorts last under DESC.
    rows = state.sql(
        "SELECT cash, positions_json FROM portfolio_snapshots ORDER BY as_of DESC, id DESC LIMIT 1"
    )
    if not rows:
        return Portfolio(cash=initial_cash, positions={})
    row = rows[0]
    return Portfolio(cash=float(row["cash"]), positions=json.loads(row["positions_json"]))


def _position_owner(
    state: SqliteState, registry: StrategyRegistry, held: Sequence[str], log: Any
) -> str | None:
    """The active strategy behind the most recent fill on a held ticker, or
    None when every strategy that bought the holdings is no longer active."""
    placeholders = ",".join("?" for _ in held)
    rows = state.sql(
        "SELECT strategy_id FROM orders"
        " WHERE status IN ('filled', 'partially_filled') AND strategy_id IS NOT NULL"
        f" AND ticker IN ({placeholders})"
        " ORDER BY updated_at DESC, created_at DESC, rowid DESC",
        list(held),
    )
    active = {h.id for h in registry.list_all(status="active")}
    for row in rows:
        if row["strategy_id"] in active:
            return row["strategy_id"]
    log.warning(
        "tick.exit.no_active_owner",
        held=list(held),
        owners=sorted({r["strategy_id"] for r in rows}),
    )
    return None


def _drop_open_order_conflicts(
    state: SqliteState, orders: list[Order], log: Any
) -> tuple[list[Order], list[dict[str, str]]]:
    """Drop orders on a (ticker, side) that already has a working order at
    the broker under another client_id (e.g. yesterday's GTC buy still
    open): the portfolio doesn't show it yet, so deciding again would double
    the position once both fill."""
    placeholders = ",".join("?" for _ in NON_TERMINAL_STATUSES)
    open_rows = state.sql(
        f"SELECT client_id, ticker, side FROM orders WHERE status IN ({placeholders})",
        list(NON_TERMINAL_STATUSES),
    )
    kept: list[Order] = []
    conflicts: list[dict[str, str]] = []
    for order in orders:
        clash = [
            r["client_id"]
            for r in open_rows
            if r["ticker"] == order.ticker
            and r["side"] == order.side
            and r["client_id"] != order.client_id
        ]
        if clash:
            log.warning(
                "tick.order.open_order_conflict",
                ticker=order.ticker,
                side=order.side,
                open_client_ids=clash,
            )
            conflicts.append({"ticker": order.ticker, "side": order.side})
            continue
        kept.append(order)
    return kept, conflicts


def _order_statuses(state: SqliteState, client_ids: Sequence[str | None]) -> dict[str, str]:
    ids = [c for c in client_ids if c]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = state.sql(
        f"SELECT client_id, status FROM orders WHERE client_id IN ({placeholders})", ids
    )
    return {r["client_id"]: r["status"] for r in rows}


def _live_order_status(state: SqliteState, client_id: str | None) -> str | None:
    """Status of an order already in the ledger that must not be placed
    again (anything but rejected/cancelled), or None when it may be placed."""
    rows = state.sql(
        "SELECT status FROM orders WHERE client_id = ? AND status NOT IN ('rejected', 'cancelled')",
        [client_id],
    )
    return rows[0]["status"] if rows else None


def _record_order(state: SqliteState, order: Order, status: OrderStatus) -> None:
    # ON CONFLICT DO UPDATE lets a retried tick progress the status of a
    # previously-``rejected`` order to ``filled`` if the re-submission
    # succeeds. The ``WHERE status IS NOT excluded.status`` guard avoids
    # rewriting ``updated_at`` when the status is unchanged (a retried
    # tick replaying an already-filled order).
    now = _iso_now()
    state.execute(
        """
        INSERT INTO orders
            (client_id, tick_id, strategy_id, ticker, side, quantity,
             order_type, limit_price, status, broker_order_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        ON CONFLICT (client_id) DO UPDATE SET
            status = excluded.status,
            updated_at = excluded.updated_at
          WHERE orders.status IS NOT excluded.status
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


def _record_fill(state: SqliteState, fill: Fill) -> None:
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
            (tick_id, as_of, taken_at, cash, positions_json, total_value)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            tick_id,
            as_of.isoformat(),
            _iso_now(),
            portfolio.cash,
            json.dumps(portfolio.positions, sort_keys=True),
            total,
        ],
    )


def _close_tick(state: SqliteState, tick_id: str, status: TickStatus, summary: dict) -> None:
    # The tick_runs CHECK constraint accepts 'running' | 'ok' | 'partial' | 'error'.
    # 'noop' is a TickResult-only distinction (no candidates were ranked); persist
    # it as 'ok' and leave the "it was a no-op" signal in summary_json.
    db_status: _DBTickStatus = "ok" if status == "noop" else status
    state.execute(
        "UPDATE tick_runs SET finished_at=?, status=?, summary_json=? WHERE id=?",
        [_iso_now(), db_status, json.dumps(summary, sort_keys=True), tick_id],
    )
