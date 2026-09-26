"""Production tick — one-shot entrypoint invoked by an external scheduler.

Each invocation is a fresh process. State lives in SqliteState + DuckDBLake;
the tick is stateless across runs. Orders are idempotent via a deterministic
``client_id`` derived from (as_of date, strategy_id, ticker, side) — not from
the per-run ``tick_id``, which stays unique so every run gets its own
``tick_runs`` row. Re-running a crashed tick for the same ``as_of`` therefore
reproduces the same client_ids, and any order already recorded as ``filled``
is skipped instead of being submitted (and applied to the portfolio) again.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Fill, Order, OrderStatus, Portfolio
from stonks.execution.orders import make_client_id
from stonks.logging import get_logger
from stonks.notify import Notification, Notifier
from stonks.production.ranker import Ranker
from stonks.production.risk import RiskPolicy, apply_risk
from stonks.production.shadow import evaluate_shadow_strategies
from stonks.registry.store import StrategyRegistry
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

TickStatus = Literal["ok", "partial", "error", "noop"]
_DBTickStatus = Literal["running", "ok", "partial", "error"]

_log = get_logger("stonks.production.tick")


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
) -> TickResult:
    # 1. rank active strategies; prices are needed by both the real and the
    #    shadow path, so load them up front.
    ranker = Ranker(
        registry=registry,
        lake=lake,
        universe=settings.universe,
        threshold=settings.threshold,
    )
    ranked = ranker.rank(as_of=as_of)
    prices = _current_prices(
        lake,
        settings.universe,
        as_of,
        max_staleness_days=settings.max_price_staleness_days,
    )

    if not ranked:
        summary: dict[str, Any] = {"reason": "no_candidates"}
        summary.update(
            _shadow_phase(state, lake, registry, settings, as_of, dry_run, tick_id, prices, log)
        )
        _close_tick(state, tick_id, status="noop", summary=summary)
        log.info("tick.noop", reason="no_candidates")
        return TickResult(
            tick_id=tick_id,
            status="noop",
            winner_strategy_id=None,
            orders_placed=0,
            fills=0,
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

    # 2. prepare portfolio
    portfolio = _load_or_seed_portfolio(state, initial_cash=settings.initial_cash)

    # 3. decide, then let the risk layer clip/drop before anything reaches
    #    the broker.
    proposed = strategy.decide(my_picks, portfolio, prices, as_of)
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

    broker = _build_broker(portfolio, settings, prices, as_of)
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
    sells = [o for o in orders_with_tick if o.side == "sell"]
    buys = [o for o in orders_with_tick if o.side == "buy"]

    placed = 0
    fills_count = 0
    any_failure = False
    # Broker outcomes are buffered and persisted together with the portfolio
    # snapshot in one transaction, so a crash can't leave fills recorded
    # without the snapshot that reflects them.
    outcomes: list[tuple[Order, OrderStatus, Fill | None]] = []

    def place(batch: list[Order]) -> None:
        nonlocal placed, fills_count, any_failure
        for order in batch:
            if dry_run:
                placed += 1
                continue
            if _already_filled(state, order.client_id):
                log.info("tick.order.skipped_already_filled", client_id=order.client_id)
                continue
            try:
                fill = broker.place_order(order)
            except Exception as exc:
                any_failure = True
                log.warning("tick.order.failed", ticker=order.ticker, error=str(exc))
                continue
            outcomes.append((order, "filled" if fill else "rejected", fill))
            placed += 1
            if fill is not None:
                fills_count += 1

    # Sells first. The first risk pass counted their expected proceeds, so
    # buys are re-checked against the portfolio as it stands after the
    # sells: a rejected or unfilled sell must not fund a buy.
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

    if not dry_run:
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
    shadow_summary = _shadow_phase(
        state, lake, registry, settings, as_of, dry_run, tick_id, prices, log
    )

    status: TickStatus = "ok" if not any_failure else "partial"
    _close_tick(
        state,
        tick_id,
        status=status,
        summary={
            "winner_strategy_id": winner_id,
            "winner_expected_return": winner_return,
            "orders_placed": placed,
            "fills": fills_count,
            "risk_adjustments": [a.as_dict() for a in risk_adjustments],
            **shadow_summary,
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


def _build_broker(
    portfolio: Portfolio,
    settings: TickSettings,
    prices: dict[str, float],
    as_of: date,
) -> SimulatedBroker:
    """The one place the tick constructs its broker; swap here for a real
    broker (roadmap 2.6)."""
    broker = SimulatedBroker(
        portfolio=portfolio,
        slippage_bps=settings.slippage_bps,
        fee_per_trade=settings.fee_per_trade,
    )
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
    prices: dict[str, float],
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
        outcomes = evaluate_shadow_strategies(
            state,
            registry,
            shadow_ranked,
            prices,
            _asset_classes(lake, settings.universe),
            as_of,
            tick_id,
            settings,
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
        "SELECT cash, positions_json FROM portfolio_snapshots "
        "ORDER BY as_of DESC, id DESC LIMIT 1"
    )
    if not rows:
        return Portfolio(cash=initial_cash, positions={})
    row = rows[0]
    return Portfolio(cash=float(row["cash"]), positions=json.loads(row["positions_json"]))


def _current_prices(
    lake: DuckDBLake,
    universe: Sequence[str],
    as_of: date,
    *,
    max_staleness_days: int = 7,
) -> dict[str, float]:
    if not universe:
        return {}
    # Single grouped query: per ticker, take the latest close at or before
    # ``as_of`` but no older than ``max_staleness_days`` calendar days.
    # Tickers with only older closes are dropped (no price → no trade).
    # Avoids the N+1 pattern of one LIMIT-1 query per ticker.
    oldest = as_of - timedelta(days=max_staleness_days)
    df = lake.sql(
        """
        SELECT ticker, close
          FROM prices
         WHERE ticker = ANY(?) AND date <= ? AND date >= ?
         QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) = 1
        """,
        [list(universe), as_of, oldest],
    )
    if df.empty:
        return {}
    return {row.ticker: float(row.close) for row in df.itertuples(index=False)}


def _already_filled(state: SqliteState, client_id: str | None) -> bool:
    rows = state.sql(
        "SELECT 1 FROM orders WHERE client_id = ? AND status = 'filled'",
        [client_id],
    )
    return bool(rows)


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
