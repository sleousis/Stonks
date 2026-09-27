"""Production tick — one-shot entrypoint invoked by an external scheduler.

Each invocation is a fresh process. State lives in SqliteState + DuckDBLake;
the tick is stateless across runs. A tick has three phases (BL-12, design
``docs/design/accounts-and-modes.md`` section 5):

1. **Signal phase.** Every active strategy is scored once over the universe
   (:class:`~stonks.production.ranker.Ranker`); shadow strategies are scored
   once too when a book or the model books need them. The instance that
   scored is the instance that decides (:class:`StrategyPool`), so per-day
   state from ``estimate_return`` reaches ``decide``.
2. **Portfolio phase.** For each book of the :class:`TickPlan` (by default
   one: the default portfolio ``pf_default`` over every active strategy,
   today's single book), the construction pipeline
   (:func:`stonks.portfolio.pipeline.build_orders`) turns that book's
   signals into orders: its constructor (``single_winner`` by default), its
   risk policy, then its broker. Orders, fills and snapshots carry the
   book's ``portfolio_id``; position attribution and other
   ``"portfolio"``-stage hooks run in the transaction that writes them.
   With several books, each one soft-fails alone.
3. **Model books and hooks.** The shadow strategies' model books advance
   (``production.shadow``), strictly after the real ledgers committed, then
   the ``"tick"``-stage hooks run (``production.hooks``).

Orders are idempotent via a deterministic ``client_id`` derived from
(as_of date, portfolio, strategy_id, ticker, side), not from the per-run
``tick_id``, which stays unique so every run gets its own ``tick_runs`` row.
The default portfolio keeps the pre-accounts format
``<as_of>:<strategy>:<ticker>:<side>`` so a same-day re-run across the
upgrade still recognises its orders; every other portfolio's ids read
``<as_of>:<portfolio_id>:<strategy>:<ticker>:<side>``. Re-running a crashed
tick for the same ``as_of`` therefore reproduces the same client_ids, and
any order already in ``orders`` (unless ``rejected`` / ``cancelled``) is
skipped instead of being submitted (and applied to the portfolio) again.

The broker is the simulated one unless ``TickSettings.broker_kind`` opts in
to an external broker (``alpaca``, built by ``broker_factory``) for the
default portfolio. Then the tick reconciles open orders before deciding,
takes the portfolio from the broker account, commits each order row as
``pending`` *before* submitting it (so a crash never leaves a submitted
order unrecorded) and books statuses and fills only through
``execution.reconcile``, from the broker's state looked up by client id.
Other broker portfolios wait for their connection's trading adapter (S3/S6)
and are skipped.

Corporate actions: before anything decides, splits and cash dividends whose
ex-date falls after the stored snapshot's ``as_of`` and on or before this
tick's ``as_of`` are applied to the stored portfolio (simulated broker; an
external broker's account already reflects them) and splits to orders
still working from earlier ticks, exactly once per event; see
``production.corporate_actions``. Valuing and sizing stay on raw quotes.

A database still on the pre-accounts schema (no ``portfolio_id`` columns)
runs the default book only and writes the pre-accounts columns.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal, get_args

from stonks.accounts.book import BookSpec
from stonks.accounts.default_book import align_default_book
from stonks.accounts.models import DEFAULT_PORTFOLIO_ID, Mode, Subscription
from stonks.accounts.models import Portfolio as AccountPortfolio
from stonks.accounts.paper import ensure_paper_account
from stonks.backtest.costs import CostModel, CostModelSettings, FixedCostModel
from stonks.backtest.simulated_broker import FinancingEvent, SimulatedBroker
from stonks.core.interval import Interval
from stonks.core.protocols import Broker, Strategy
from stonks.core.types import Fill, Order, OrderStatus, Portfolio
from stonks.execution.borrow import BorrowSource
from stonks.execution.brokers.base import (
    BrokerKind,
    BrokerMode,
    MarginPreviewer,
    OrderRejectedError,
    OrderStateSource,
)
from stonks.execution.brokers.simulated import SimulatedCosts
from stonks.execution.order_state import mark_unknown, write_state
from stonks.execution.orders import SideToken, make_client_id
from stonks.execution.reconcile import (
    NON_TERMINAL_STATUSES,
    fill_live_values,
    order_live_values,
    reconcile_order,
    startup_reconcile,
)
from stonks.factors.style import safe_style_exposures, uses_style_model
from stonks.logging import get_logger
from stonks.notify import Notification, Notifier
from stonks.portfolio import returns as portfolio_returns
from stonks.portfolio.pipeline import (
    PORTFOLIO_STRATEGY,
    BookInput,
    ConstructionSettings,
    FillCosts,
    MarketView,
    apply_book_risk,
    build_orders,
    vols_from_history,
)
from stonks.production.auto_pause import (
    broker_error_reason,
    is_short_outage,
    pause_auto,
    strategy_not_active_reason,
)
from stonks.production.corporate_actions import (
    CorporateActionPlan,
    adjust_orders_for_splits,
    adjust_working_orders,
    apply_corporate_actions,
    apply_plan,
    event_as_dict,
    ledger_enabled,
    load_corporate_actions,
    plan_corporate_actions,
    record_as_dict,
    record_plan,
    working_orders,
)
from stonks.production.decay import DecaySettings
from stonks.production.financing import last_accrual, record_accrual, short_account
from stonks.production.halts import active_halts
from stonks.production.hooks import (
    GateContext,
    NotifySignal,
    PortfolioHookContext,
    TickHookContext,
    run_gates,
    run_portfolio_hooks,
    run_tick_hooks,
)
from stonks.production.hooks.attribution import load_attribution
from stonks.production.ledger import ledger_columns, ledger_filter
from stonks.production.live.context import LiveContext
from stonks.production.live.runaway import runaway_reason
from stonks.production.live.settings import LiveSettings
from stonks.production.monitor_settings import RiskMonitorSettings
from stonks.production.ownership import (
    drop_unowned_crossings,
    managed_view,
    manual_positions,
    merge_holdings,
    owned_positions,
    strip_holdings,
)
from stonks.production.portfolio_runs import PortfolioRun, record_run, runs_recorded
from stonks.production.prices import PriceBook, held_tickers, load_history, load_prices
from stonks.production.quit_rule import QuitRuleSettings
from stonks.production.ranker import Ranker, SignalSet, StrategyPool
from stonks.production.risk import RiskPolicy, build_risk_context, needs_risk_context
from stonks.production.rules import RiskAdjustment, RiskContext
from stonks.production.shadow import (
    evaluate_books,
    evaluate_shadow_strategies,
    shadow_held_tickers,
)
from stonks.production.signals import record_signals, signals_recorded
from stonks.production.tca import annotate_orders, decision_values, tca_recorded
from stonks.production.tickets import (
    hard_to_borrow_orders,
    submit_window,
    sync_submitted,
    ticket_hold,
    tickets_recorded,
    write_tickets,
)
from stonks.production.version_books import (
    VersionBook,
    active_version_books,
    version_held_tickers,
)
from stonks.registry.store import StrategyRegistry
from stonks.store.lake import DuckDBLake
from stonks.store.pit import PitSession
from stonks.store.state import SqliteState
from stonks.strategies._common import decision_interval

TickStatus = Literal["ok", "partial", "error", "noop"]
_DBTickStatus = Literal["running", "ok", "partial", "error"]

_log = get_logger("stonks.production.tick")

#: Builds the tick's broker around the portfolio it trades (a simulated
#: broker trades that object in memory; an external one ignores it).
BrokerFactory = Callable[[Portfolio], Broker]

#: Opens the trading adapter of a broker portfolio's connection (auto mode).
TraderFactory = Callable[[AccountPortfolio], Broker]

#: What a book trades: ``legacy`` (the single default book), ``paper`` (a
#: simulated account) or ``auto`` (a broker account).
BookMode = Literal["legacy", "paper", "auto"]

#: Halt kinds that count as breaking a book's risk limits (the auto gate's
#: paper days restart after one).
_BREACH_KINDS = frozenset({"month_loss", "week_loss", "drawdown"})

#: Quantities closer than this are equal (a fill of the whole order).
_QTY_EPSILON = 1e-9

#: Bars of daily history the volatility-aware constructors read.
_VOL_HISTORY_BARS = 260


class BackdatedTickError(ValueError):
    """A non-dry-run tick was asked to trade a date earlier than the latest
    portfolio snapshot. Trading it would apply today's portfolio to an old
    date and write a new "latest" snapshot that belongs in the past.
    Entrypoints treat every refused tick date as this error."""


class FutureTickError(BackdatedTickError):
    """A non-dry-run tick was asked to trade a date after today (UTC). Its
    snapshot would block every real tick until that date, and its run
    would count as a paper day that never happened (BE-27)."""


@dataclass(frozen=True)
class TickSettings:
    universe: Sequence[str]
    threshold: float = 0.0
    initial_cash: float = 10_000.0
    #: Legacy flat costs of the simulated broker and the risk layer's cash
    #: estimate. Only used when ``costs`` is None.
    slippage_bps: float = 0.0
    fee_per_trade: float = 0.0
    #: The simulated broker's cost model (``[backtest.costs]``, the one
    #: backtests use); the risk layer's cash estimate uses it too. Exclusive with ``slippage_bps`` / ``fee_per_trade``:
    #: ``build_tick_settings`` resolves the precedence.
    costs: CostModelSettings | None = None
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
    #: Whose money the default book trades (``simulated``, ``paper`` or
    #: ``live``), recorded on every tick summary. ``None`` resolves from
    #: ``broker_kind``: ``simulated``, else ``live``, so an unknown external
    #: account never reads as paper money.
    broker_mode: BrokerMode | None = None
    #: Fraction of each cash dividend withheld as tax (0 = credit in full).
    dividend_withholding_rate: float = 0.0
    #: ``[production.construction]``: the global constructor and no-trade
    #: buffer; a portfolio's ``construction_json`` is merged on top.
    construction: ConstructionSettings = field(default_factory=ConstructionSettings)
    #: Which strategies keep a model book: ``"shadow"`` (today) or ``"all"``
    #: non-retired strategies (design section 5; go-live then reads them).
    model_books: Literal["shadow", "all"] = "shadow"
    #: ``[production.quit_rule]``: read by the ``quit_rule`` tick hook.
    quit_rule: QuitRuleSettings = field(default_factory=QuitRuleSettings)
    #: ``[production.risk_monitor]`` and ``[production.decay]``: read by the
    #: ``risk_monitor`` tick hook.
    risk_monitor: RiskMonitorSettings = field(default_factory=RiskMonitorSettings)
    decay: DecaySettings = field(default_factory=DecaySettings)
    #: A scoped tick (explicit tickers, e.g. a crypto-only job) trades only
    #: tickers of ``universe``: holdings outside it are marked but never
    #: traded, not even sold (TO-04). The full tick over the configured
    #: universe is unscoped, so a holding that left the universe is sold.
    scoped: bool = False
    #: Per ticker, the daily bar a scheduled tick must see before it buys
    #: (the session that closed by the fire time, TO-10). A ticker whose
    #: latest bar is older (the price ingest failed) is marked and sellable
    #: but not buyable. None (manual ticks): the staleness window only.
    bars_due: Mapping[str, date] | None = None
    #: Worker processes of the signal phase (``production.scoring``); 1
    #: scores in this process.
    scoring_workers: int = 1
    #: Fewest estimates (opted-in strategies x tickers) worth a pool.
    parallel_min_estimates: int = 2000
    #: The stored universe the full tick trades (``[production].universe``
    #: as an id): the ranker skips names that are not members on the tick
    #: date (BL-49). ``None`` for a ticker list or a scoped tick.
    universe_id: str | None = None
    #: ``[production.live]``: live books may decide now and submit in the
    #: window before the next open (order tickets, roadmap 19.8).
    live: LiveSettings = field(default_factory=LiveSettings)

    def __post_init__(self) -> None:
        self.simulated_costs  # noqa: B018 - validates costs vs legacy (not both)
        simulated = self.broker_kind == "simulated"
        if self.broker_mode is None:
            object.__setattr__(self, "broker_mode", "simulated" if simulated else "live")
        elif (self.broker_mode == "simulated") != simulated:
            raise ValueError(
                f"broker_mode={self.broker_mode!r} contradicts broker_kind={self.broker_kind!r}"
            )
        if not 0.0 <= self.dividend_withholding_rate <= 1.0:
            raise ValueError(
                f"dividend_withholding_rate must be in [0, 1], got {self.dividend_withholding_rate}"
            )

    @property
    def simulated_costs(self) -> SimulatedCosts:
        return SimulatedCosts(
            model=self.costs, slippage_bps=self.slippage_bps, fee_per_trade=self.fee_per_trade
        )

    @property
    def cost_model(self) -> CostModel:
        """The model the simulated broker fills through; TCA records its
        estimate on every order as the modelled cost (BL-32)."""
        if self.costs is not None:
            return self.costs.build()
        return FixedCostModel(self.slippage_bps, self.fee_per_trade)

    @property
    def fill_costs(self) -> FillCosts:
        return FillCosts(
            slippage_bps=self.slippage_bps, fee_per_trade=self.fee_per_trade, model=self.costs
        )


@dataclass(frozen=True)
class TickBook:
    """One book the tick trades.

    ``legacy``: the single book of an install whose subscriptions aren't
    wired into the tick yet: every active strategy, equal weight, the global
    risk policy and constructor (``spec.strategy_weights`` is ``None``).
    Otherwise the book's strategies are its subscriptions."""

    spec: BookSpec
    owner_id: str | None = None
    #: Subscription id per strategy (for attribution).
    subscription_ids: Mapping[str, str] = field(default_factory=dict)
    legacy: bool = False
    #: ``paper`` or ``auto`` for a subscription book (``None``: legacy).
    mode: Literal["paper", "auto"] | None = None
    #: The broker portfolio an auto book trades through its connection.
    account: AccountPortfolio | None = None
    #: For a broker portfolio's paper account: that broker portfolio.
    parent_id: str | None = None

    @property
    def portfolio_id(self) -> str:
        return self.spec.portfolio_id

    @property
    def run_mode(self) -> BookMode:
        if self.legacy or self.mode is None:
            return "legacy"
        return self.mode

    def subscriptions_in(self, mode: Mode) -> list[str]:
        """Ids of this book's subscriptions in ``mode``."""
        return sorted(
            sid
            for strategy, sid in self.subscription_ids.items()
            if self.spec.strategy_modes.get(strategy) is mode
        )


@dataclass(frozen=True)
class TickPlan:
    """The books one tick trades and the notify-mode subscriptions it
    records signals for."""

    books: tuple[TickBook, ...]
    notify: tuple[Subscription, ...] = ()
    #: Opens a connection's trading adapter for auto books (``None``: auto
    #: books other than the legacy live default are skipped).
    traders: TraderFactory | None = field(default=None, compare=False)
    #: Problems the plan found that an operator should hear about (BE-10).
    notices: tuple[str, ...] = ()

    @classmethod
    def default(cls, settings: TickSettings) -> TickPlan:
        """Today's single book: ``pf_default`` over every active strategy."""
        spec = BookSpec(
            portfolio_id=DEFAULT_PORTFOLIO_ID,
            risk=settings.risk,
            broker="connection" if settings.broker_kind != "simulated" else "simulated",
            initial_cash=float(settings.initial_cash),
        )
        return cls(books=(TickBook(spec=spec, legacy=True),))


@dataclass(frozen=True)
class BookResult:
    portfolio_id: str
    status: TickStatus
    winner_strategy_id: str | None
    orders_placed: int
    fills: int
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TickResult:
    tick_id: str
    status: TickStatus
    winner_strategy_id: str | None
    orders_placed: int
    fills: int
    portfolios: tuple[BookResult, ...] = ()
    dry_run: bool = False
    broker_mode: BrokerMode = "simulated"


def run_tick(
    state: SqliteState,
    lake: DuckDBLake,
    registry: StrategyRegistry,
    settings: TickSettings,
    as_of: date | None = None,
    dry_run: bool = False,
    notifier: Notifier | None = None,
    broker_factory: BrokerFactory | None = None,
    plan: TickPlan | None = None,
) -> TickResult:
    """``plan``: the books to trade (default :meth:`TickPlan.default`; build
    one from portfolios and subscriptions with :func:`load_tick_plan`)."""
    as_of = as_of or utc_today()
    tick_id = _new_tick_id(as_of)
    started = _iso_now()
    log = _log.bind(tick_id=tick_id, as_of=as_of.isoformat(), dry_run=dry_run)
    plan = plan or TickPlan.default(settings)
    if not dry_run:
        today = utc_today()
        if as_of > today:
            raise FutureTickError(
                f"as_of {as_of.isoformat()} is after today ({today.isoformat()}); "
                "only a dry run may look ahead"
            )
        _refuse_backdated(state, as_of, log, [b.portfolio_id for b in plan.books])

    state.execute(
        "INSERT INTO tick_runs (id, started_at, status, summary_json) VALUES (?, ?, 'running', ?)",
        [tick_id, started, json.dumps({OWNER_KEY: tick_owner()})],
    )
    _RUNNING.add(tick_id)

    # Any failure past this point closes the tick as 'error' so the ledger
    # never keeps a row stuck at 'running'; the exception still propagates.
    try:
        # RS-03: the tick decides on daily bars, so a strategy sees a bar of
        # any interval only once it has closed by the day's close (a 24/7
        # market's midnight decision would otherwise read like an intraday one).
        with decision_interval(Interval.DAY_1):
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
                plan=plan,
            )
    except Exception as exc:
        log.error("tick.error", error=str(exc), error_type=type(exc).__name__)
        try:
            _close_tick(
                state,
                tick_id,
                status="error",
                summary={
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    **_run_facts(settings, dry_run),
                },
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
    finally:
        _RUNNING.discard(tick_id)
    if result.status == "partial":
        _safe_notify(notifier, _partial_notification(result, as_of), log)
    if not dry_run:
        for notice in plan.notices:
            log.warning("tick.plan_notice", notice=notice)
            _safe_notify(
                notifier,
                Notification(
                    level="warning",
                    title="default book unmanaged",
                    message=notice,
                    fields={"tick_id": tick_id, "as_of": as_of.isoformat()},
                ),
                log,
            )
    return result


def _partial_notification(result: TickResult, as_of: date) -> Notification:
    """The operator alert of a partial tick: the books that failed with
    their errors (TO-12), and those whose orders raised at the broker."""
    failed = {
        b.portfolio_id: f"{b.summary.get('error_type', 'Error')}: {b.summary.get('error', '')}"
        for b in result.portfolios
        if b.status == "error"
    }
    raised = [b.portfolio_id for b in result.portfolios if b.status == "partial"]
    parts = [f"book {pid} failed: {err}" for pid, err in failed.items()]
    if raised or not failed:
        parts.append("one or more orders raised at the broker; see logs")
    fields: dict[str, Any] = {
        "tick_id": result.tick_id,
        "as_of": as_of.isoformat(),
        "status": result.status,
    }
    if failed:
        fields["failed_portfolios"] = failed
    if raised:
        fields["broker_errors_in"] = raised
    return Notification(
        level="warning", title="tick partially failed", message="; ".join(parts), fields=fields
    )


# ---- the tick ----------------------------------------------------------------


@dataclass
class _TickRun:
    """What every book of one tick shares."""

    state: SqliteState
    lake: DuckDBLake
    registry: StrategyRegistry
    settings: TickSettings
    as_of: date
    dry_run: bool
    tick_id: str
    log: Any
    notifier: Notifier | None
    broker_factory: BrokerFactory | None
    plan: TickPlan
    #: The ledger tables carry ``portfolio_id`` (accounts schema).
    scoped: bool
    signals: SignalSet
    pool: StrategyPool
    #: Ids of the strategies that are ``active`` right now (auto needs one).
    active: frozenset[str] = frozenset()
    #: Every registered strategy's status right now.
    statuses: Mapping[str, str] = field(default_factory=dict)
    _shadow: SignalSet | None = None
    _shadow_error: Exception | None = None
    _vols: dict[str, float] | None = None
    _pit: PitSession | None = None

    def shadow_signals(self) -> SignalSet:
        """Shadow strategies scored once per tick (raises what the scoring
        raised, every time it is asked)."""
        if self._shadow is None and self._shadow_error is None:
            try:
                self._shadow = Ranker(
                    registry=self.registry,
                    lake=self.lake,
                    universe=self.settings.universe,
                    threshold=self.settings.threshold,
                    status="shadow",
                    workers=self.settings.scoring_workers,
                    min_parallel_estimates=self.settings.parallel_min_estimates,
                    universe_id=self.settings.universe_id,
                ).score(as_of=self.as_of)
            except Exception as exc:
                self._shadow_error = exc
            else:
                self.pool.add(self._shadow)
        if self._shadow_error is not None:
            raise self._shadow_error
        assert self._shadow is not None
        return self._shadow

    def all_signals(self) -> SignalSet:
        """Active signals plus shadow ones (when they could be scored)."""
        try:
            return self.signals.merged(self.shadow_signals())
        except Exception as exc:
            self.log.error("tick.shadow_signals_failed", error=str(exc))
            return self.signals

    def vols(self, tickers: Sequence[str]) -> dict[str, float]:
        """Annual volatilities from daily history up to ``as_of``."""
        if self._vols is None:
            universe = sorted({*self.settings.universe, *tickers})
            history = load_history(self.lake, universe, self.as_of, bars=_VOL_HISTORY_BARS)
            self._vols = vols_from_history(history)
        missing = [t for t in tickers if t not in self._vols]
        if missing:
            history = load_history(self.lake, missing, self.as_of, bars=_VOL_HISTORY_BARS)
            self._vols.update(vols_from_history(history))
        return self._vols

    def market_history(
        self, tickers: Collection[str], lookback: int
    ) -> portfolio_returns.MarketHistory:
        """Daily returns up to ``as_of`` through one point-in-time session
        per tick, so every book reads each ticker's bars once (P12)."""
        if self._pit is None:
            self._pit = PitSession(self.lake)
        view = self._pit.at(self.as_of, decision_interval=Interval.DAY_1)
        return portfolio_returns.market_history(view, tickers, lookback=lookback)


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
    plan: TickPlan,
) -> TickResult:
    scoped = _ledger_scoped(state)
    if not scoped and any(not b.legacy for b in plan.books):
        raise ValueError("portfolio books need the accounts schema; run `stonks db init`")

    # 1. signal phase: every active strategy scored once.
    ranker = Ranker(
        registry=registry,
        lake=lake,
        universe=settings.universe,
        threshold=settings.threshold,
        workers=settings.scoring_workers,
        min_parallel_estimates=settings.parallel_min_estimates,
        allow_short=any(b.spec.allow_short for b in plan.books),
        universe_id=settings.universe_id,
    )
    signals = ranker.score(as_of=as_of)
    pool = StrategyPool(registry, lake)
    pool.add(signals)
    run = _TickRun(
        state=state,
        lake=lake,
        registry=registry,
        settings=settings,
        as_of=as_of,
        dry_run=dry_run,
        tick_id=tick_id,
        log=log,
        notifier=notifier,
        broker_factory=broker_factory,
        plan=plan,
        scoped=scoped,
        signals=signals,
        pool=pool,
        statuses=(statuses := {h.id: h.status for h in registry.list_all()}),
        active=frozenset(sid for sid, st in statuses.items() if st == "active"),
    )
    _expect_consumers(run)

    # 2. portfolio phase: one book at a time, each in its own transactions.
    results: list[BookResult] = []
    single = len(plan.books) == 1
    for book in plan.books:
        started_at = _iso_now()
        try:
            outcome = _run_book(run, book)
        except Exception as exc:
            # The default book alone fails the tick as the old single book
            # did (the entrypoint reports the error), other books are
            # isolated from each other.
            if single and (book.legacy or book.portfolio_id == DEFAULT_PORTFOLIO_ID):
                raise
            log.error(
                "tick.portfolio_failed",
                portfolio_id=book.portfolio_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            outcome = BookResult(
                portfolio_id=book.portfolio_id,
                status="error",
                winner_strategy_id=None,
                orders_placed=0,
                fills=0,
                summary={"error": str(exc), "error_type": type(exc).__name__},
            )
            outcome = _pause_on_broker_error(run, book, outcome, exc)
        else:
            if outcome.status == "partial":
                outcome = _pause_on_broker_error(
                    run, book, outcome, "an order raised at the broker"
                )
        outcome = _pause_inactive_auto(run, book, outcome)
        results.append(outcome)
        _record_portfolio_run(run, book, outcome, started_at)

    # 3. model books, strictly after the real ledgers committed, then hooks.
    shadow_summary = _shadow_phase(run)
    _record_signal_phase(run)
    notify_signals = _notify_signals(run)
    hook_summary = run_tick_hooks(
        TickHookContext(
            state=state,
            lake=lake,
            tick_id=tick_id,
            as_of=as_of,
            dry_run=dry_run,
            signals=signals,
            portfolios={r.portfolio_id: r.summary for r in results},
            notify_signals=notify_signals,
            settings=settings,
            registry=registry,
        ),
        log,
    )

    status = _tick_status(results)
    if single:
        [only] = results
        # the book's keys sit at the top level; name whose they are, so
        # readers see them only for their own portfolio (AS-02)
        summary = {"portfolio_id": only.portfolio_id, **only.summary}
        winner = only.winner_strategy_id
    else:
        summary = {
            "portfolios": {r.portfolio_id: {"status": r.status, **r.summary} for r in results},
            "orders_placed": sum(r.orders_placed for r in results),
            "fills": sum(r.fills for r in results),
        }
        winner = None
    summary.update(shadow_summary)
    summary.update(hook_summary)
    summary.update(_run_facts(settings, dry_run))
    _close_tick(state, tick_id, status=status, summary=summary)
    if status == "noop":
        log.info("tick.noop", reason=summary.get("reason"))
    return TickResult(
        tick_id=tick_id,
        status=status,
        winner_strategy_id=winner,
        orders_placed=sum(r.orders_placed for r in results),
        fills=sum(r.fills for r in results),
        portfolios=tuple(results),
        dry_run=dry_run,
        broker_mode=_broker_mode(settings),
    )


def _broker_mode(settings: TickSettings) -> BrokerMode:
    assert settings.broker_mode is not None  # resolved in __post_init__
    return settings.broker_mode


def _run_facts(settings: TickSettings, dry_run: bool) -> dict[str, Any]:
    """The summary keys that say how the tick ran: a dry run or not, and
    whose money its default book traded."""
    return {"dry_run": dry_run, "broker_mode": _broker_mode(settings)}


def _tick_status(results: Sequence[BookResult]) -> TickStatus:
    if len(results) == 1:
        return results[0].status
    if any(r.status in ("error", "partial") for r in results):
        return "partial"
    if all(r.status == "noop" for r in results):  # also: no book to trade
        return "noop"
    return "ok"


def trades_live(book: TickBook, settings: TickSettings) -> bool:
    """The book trades at an external broker (today only the default
    portfolio, through ``[brokers].kind``)."""
    return book.portfolio_id == DEFAULT_PORTFOLIO_ID and settings.broker_kind != "simulated"


def book_strategies(
    book: TickBook,
    scored: Sequence[str],
    settings: TickSettings,
    active: Collection[str] | None = None,
) -> list[str]:
    """The strategies whose signals a book trades. The legacy book trades
    every scored (active) strategy. A subscription book trades its paper,
    approve and auto subscriptions, except at a live broker, where only
    approve and auto ones place orders: paper money must never reach a real
    account. An approve or auto subscription trades only while its strategy
    is ``active`` (BE-01; ``active`` names them, ``None`` skips the check)."""
    if book.legacy or book.spec.strategy_weights is None:
        return list(scored)
    live = trades_live(book, settings)
    modes = book.spec.strategy_modes

    def at_broker(s: str) -> bool:
        mode = modes.get(s)
        return mode is not None and mode.trades_live

    return [
        s
        for s, w in book.spec.strategy_weights.items()
        if w > 0
        and (not live or at_broker(s))
        and (active is None or not at_broker(s) or s in active)
    ]


def inactive_auto(book: TickBook, active: Collection[str]) -> list[str]:
    """Strategies of ``book``'s auto subscriptions that are not active."""
    if book.legacy or book.spec.strategy_weights is None:
        return []
    modes = book.spec.strategy_modes
    return sorted(s for s in book.spec.strategy_weights if modes.get(s) is Mode.AUTO
                  and s not in active)  # fmt: skip


def _book_strategies(run: _TickRun, book: TickBook) -> list[str]:
    return book_strategies(book, list(run.signals.scores), run.settings, run.active)


def _needs_shadow_signals(run: _TickRun) -> bool:
    wanted = {s for b in run.plan.books if not b.legacy for s in _book_strategies(run, b)}
    wanted |= {s.strategy_id for s in run.plan.notify}
    return bool(wanted - set(run.signals.scores))


def _expect_consumers(run: _TickRun) -> None:
    """Declare how many consumers will decide with each strategy, so the
    pool copies a strategy before its first decision when others follow."""
    for book in run.plan.books:
        for sid in _book_strategies(run, book):
            run.pool.expect(sid)
    if run.dry_run or not run.settings.shadow_enabled:
        return
    for handle in run.registry.list_all(status="shadow"):
        run.pool.expect(handle.id)
    if run.settings.model_books == "all":
        for sid in run.signals.scores:
            run.pool.expect(sid)


def _run_book(run: _TickRun, book: TickBook) -> BookResult:
    state, lake, settings, as_of, log = run.state, run.lake, run.settings, run.as_of, run.log
    tick_id, dry_run = run.tick_id, run.dry_run
    portfolio_id = book.portfolio_id
    scope = portfolio_id if run.scoped else None
    log = log.bind(portfolio_id=portfolio_id) if not book.legacy else log
    universe = (
        list(book.spec.universe) if book.spec.universe is not None else list(settings.universe)
    )
    is_default = portfolio_id == DEFAULT_PORTFOLIO_ID

    # 2. prepare the portfolio, then price the universe plus every holding:
    #    a held ticker outside the universe must still be marked and sellable.
    #    An external broker (opt-in, e.g. Alpaca) is the source of truth: sync
    #    its order statuses and fills into the ledger first, then read the
    #    portfolio from the account.
    external = trades_live(book, settings)
    connection = book.spec.broker == "connection" and not external
    if connection and book.mode == "auto" and run.plan.traders and book.account:
        external = True
    elif connection:
        log.warning("tick.portfolio_skipped", reason="no_connection_broker")
        return BookResult(
            portfolio_id=portfolio_id,
            status="noop",
            winner_strategy_id=None,
            orders_placed=0,
            fills=0,
            summary={"reason": "no_connection_broker"},
        )
    factory = run.broker_factory if is_default else None
    broker: Broker | None = None
    if external:
        if connection:
            assert run.plan.traders is not None and book.account is not None
            broker = run.plan.traders(book.account)
        elif factory is None:
            raise ValueError(
                f"broker_kind={settings.broker_kind!r} needs a broker_factory "
                "(build it with production.settings_builder.build_tick_runtime)"
            )
        else:
            broker = factory(Portfolio(cash=0.0))
        if not isinstance(broker, OrderStateSource):
            raise TypeError(
                f"broker_kind={settings.broker_kind!r}: the broker must look orders up by "
                "client id (OrderStateSource) so crashed submissions can be reconciled"
            )
        if not dry_run:
            # 19.8: the startup reconciliation gate. Every open order is
            # synced by client id first; while one stays ``unknown`` (a
            # submit with no answer) the book decides nothing.
            pre = startup_reconcile(broker, state, portfolio_id=portfolio_id)
            log.info(
                "tick.reconciled",
                orders_checked=pre.summary.orders_checked,
                fills_inserted=pre.summary.fills_inserted,
            )
            if pre.unresolved:
                return _unreconciled(run, book, pre.unresolved)
            if tickets_recorded(state):
                # sent tickets follow their orders (filled, unfilled, ...)
                sync_submitted(state, now=datetime.now(UTC))
        portfolio = broker.fetch_portfolio()
    else:
        portfolio = _load_or_seed_portfolio(
            state, initial_cash=book.spec.initial_cash, portfolio_id=scope
        )

    # 2b. corporate actions since the stored snapshot, before anything sizes.
    #     Derived from the snapshot's as_of, so persisted exactly when this
    #     tick's snapshot is (see production.corporate_actions).
    since = _latest_snapshot_as_of(state, portfolio_id=scope)
    working = _working_orders(state, scope)
    actions = load_corporate_actions(lake, [*held_tickers(portfolio.positions), *working.values()])
    applied = []
    plan: CorporateActionPlan | None = None
    if scope is not None and ledger_enabled(state):
        # the per-portfolio ledger: by ex-date, once, late rows included (TO-05)
        plan = plan_corporate_actions(state, lake, actions, portfolio_id=scope, as_of=as_of)
        if not external:
            applied = apply_plan(
                portfolio, plan, withholding_rate=settings.dividend_withholding_rate
            )
        for event in plan.deferred:
            log.warning("tick.corporate_action_deferred", **event_as_dict(event))
        if plan.deferred and not dry_run:
            _safe_notify(
                run.notifier,
                Notification(
                    level="warning",
                    title="corporate actions deferred",
                    message="no bar on or after the ex-date yet; applied once it is ingested",
                    fields={
                        "tick_id": tick_id,
                        "as_of": as_of.isoformat(),
                        "portfolio_id": portfolio_id,
                        "events": [
                            f"{e.ticker} {event_as_dict(e)['kind']} {e.ex_date.isoformat()}"
                            for e in plan.deferred
                        ],
                    },
                ),
                log,
            )
    elif not external:
        applied = apply_corporate_actions(
            portfolio,
            actions,
            since=since,
            as_of=as_of,
            withholding_rate=settings.dividend_withholding_rate,
        )
    for record in applied:
        log.info("tick.corporate_action", **record_as_dict(record))
    corporate_summary: dict[str, Any] = (
        {"corporate_actions": [record_as_dict(r) for r in applied]} if applied else {}
    )
    if plan is not None and plan.deferred:
        corporate_summary["deferred_corporate_actions"] = [event_as_dict(e) for e in plan.deferred]

    def persist_corporate_actions() -> int:
        """Record the handled events and split-adjust the working orders;
        call inside the transaction that writes this tick's snapshot.
        Returns how many rows changed (a noop tick then still snapshots)."""
        now = _iso_now()
        if plan is None:
            return adjust_working_orders(
                state, list(working), actions, since=since, as_of=as_of, now=now
            )
        record_plan(state, plan, applied, portfolio_id=portfolio_id, tick_id=tick_id, now=now)
        return len(plan.due) + adjust_orders_for_splits(state, list(working), plan.splits, now=now)

    held = held_tickers(portfolio.positions)
    book_prices = load_prices(
        lake,
        universe,
        held,
        as_of,
        max_staleness_days=settings.max_price_staleness_days,
    )
    prices = book_prices.prices
    # BE-02: a connected account may hold the user's own positions. The auto
    # book decides and sizes on what it owns (its fill ledger) and never
    # trades the rest; the snapshot still marks the whole account.
    account = portfolio
    external_holdings: dict[str, float] = {}
    if connection:
        owned = owned_positions(state, portfolio_id, actions)
        portfolio, external_holdings = managed_view(account, owned)
        held = held_tickers(portfolio.positions)
        if external_holdings:
            log.info("tick.external_holdings", tickers=sorted(external_holdings))
    manual_holdings: dict[str, float] = {}
    if scope is not None and not external and not connection:
        # Roadmap 20.1: what a person bought by hand in a simulated book is
        # theirs. Strategies decide and size without it and never trade it;
        # the snapshot puts it back.
        manual = manual_positions(state, portfolio_id, actions)
        if manual:
            portfolio, manual_holdings = strip_holdings(account, manual)
            external_holdings = dict(manual_holdings)
            held = held_tickers(portfolio.positions)
            log.info("tick.manual_holdings", tickers=sorted(manual_holdings))

    def result(status: TickStatus, winner: str | None, placed: int, fills: int, summary: dict):
        return BookResult(
            portfolio_id=portfolio_id,
            status=status,
            winner_strategy_id=winner,
            orders_placed=placed,
            fills=fills,
            summary=summary,
        )

    def gate() -> Any:
        return run_gates(
            GateContext(
                state=state,
                as_of=as_of,
                portfolio_id=portfolio_id,
                owner_id=book.owner_id,
                dry_run=dry_run,
                policy=book.spec.risk,
                parent_portfolio_id=book.parent_id,
            ),
            log,
        )

    def halted(verdict: Any) -> dict[str, Any]:
        if verdict is None:
            return {}
        return {"halted": {"halt": verdict.halt, "gate": verdict.gate, "reason": verdict.reason}}

    def noop(reason: str) -> BookResult:
        halt_summary: dict[str, Any] = {}
        if not dry_run:
            # nothing trades, but the gates still record a breaker trip the
            # day it happens, and applied events must still be persisted
            halt_summary = halted(gate())
            with state.transaction():
                if persist_corporate_actions() or applied:
                    _snapshot_portfolio(state, tick_id, account, prices, as_of, portfolio_id=scope)
        log.info("tick.portfolio_noop", reason=reason)
        return result("noop", None, 0, 0, {"reason": reason, **halt_summary, **corporate_summary})

    # 3. construct: the pipeline turns this book's signals into orders
    #    (decide or targets, stale buys dropped, then the risk layer).
    strategy_ids = _book_strategies(run, book)
    signal_set = run.all_signals() if _needs_shadow_signals(run) else run.signals
    book_scores = signal_set.for_book(book.spec.allow_short)
    signals = {
        sid: _in_universe(book_scores[sid], book.spec.universe)
        for sid in book_scores
        if sid in strategy_ids
    }
    construction = (
        settings.construction
        if book.legacy
        else ConstructionSettings.from_mapping(book.spec.construction)
    )
    asset_classes = _asset_classes(lake, [*universe, *portfolio.positions])
    market = MarketView(
        as_of=as_of,
        prices=prices,
        buyable=_buyable(book_prices, settings.bars_due),
        volumes=book_prices.volumes,
        asset_classes=asset_classes,
        vols_annual=({} if construction.is_single_winner else run.vols([*universe, *held])),
    )
    lookback = portfolio_returns.returns_lookback(construction)
    if lookback is not None:
        # 9.5.1: the covariance constructors read daily returns up to as_of
        # (the market's volumes, from the priced bars, feed the impact term)
        names = {t for scores in signals.values() for t in scores} | set(held)
        market = replace(market, returns_history=run.market_history(names, lookback).returns)
        if uses_style_model(construction):
            # 22.4: the style risk model reads exposures known at as_of
            exposures = safe_style_exposures(lake, sorted(names), as_of)
            market = replace(market, factor_exposures=exposures)
    # Rules that need history (W3.1) run only with a context: built when the
    # book's policy (or a strategy slice's) enables one.
    risk_context = None
    if needs_risk_context(book.spec.risk, *book.spec.risk_overrides.values()):
        risk_context = build_risk_context(
            lake,
            state,
            portfolio,
            prices,
            as_of,
            policy=book.spec.risk,
            universe=universe,
            cost_model=settings.costs,
            volumes=book_prices.volumes,
            portfolio_id=portfolio_id,
        )
        if book.spec.allow_short:
            # the short rules read the book's own margin model and borrow
            # source, as they do in a backtest (BE-30)
            margin, borrow = short_account(book.spec.risk)
            risk_context = replace(
                risk_context, margin=margin, borrow=borrow or risk_context.borrow
            )
    if external:
        # 19.8: a book at a real broker gives the live safeguards and the
        # account rules its live context (the account, quotes, the owner's
        # allocation, today's sent notional). They do nothing without it.
        base = risk_context or RiskContext(
            portfolio=portfolio,
            prices=prices,
            asset_classes=asset_classes,
            policy=book.spec.risk,
            portfolio_id=portfolio_id,
            as_of=as_of,
            allow_short=book.spec.allow_short,
        )
        live_tickers = {t for scores in signals.values() for t in scores} | set(held)
        risk_context = replace(
            base,
            live=_live_context(run, book, broker, sorted(live_tickers), external_holdings),
        )
    book_input = BookInput(
        portfolio=portfolio,
        construction=construction,
        risk=book.spec.risk,
        strategy_weights=None if book.legacy else book.spec.strategy_weights,
        risk_overrides=book.spec.risk_overrides,
        prior_attribution=(load_attribution(state, portfolio_id, as_of) if run.scoped else {}),
        costs=settings.fill_costs,
        risk_context=risk_context,
        allow_short=book.spec.allow_short,
    )
    candidates = None if book.legacy else set(strategy_ids)

    def exit_owner() -> str | None:
        owner = _position_owner(
            state, run.registry, held, log, portfolio_id=scope, candidates=candidates
        )
        if owner is not None:
            log.info("tick.exit_decision", strategy_id=owner, held=held)
        return owner

    make_id = _client_id_fn(as_of, portfolio_id)
    pipeline = build_orders(
        signals,
        book_input,
        market,
        strategies=run.pool.checkout,
        exit_owner=exit_owner,
        client_id=make_id,
    )
    # BE-18: a retired strategy of this subscription book exits its own
    # holdings, whatever the others decided for those tickers, then its
    # subscription ends. The legacy single book keeps its old rule: holdings
    # whose owners all left active are kept (``no_active_owner``).
    members = set() if book.legacy else set(book.spec.strategy_weights or {})
    retired = sorted(sid for sid in members if run.statuses.get(sid) == "retired")
    retired_owned: dict[str, str] = {}
    exits: list[Order] = []
    if retired and run.scoped and held:
        owners = _holding_owners(state, portfolio_id, held, book_input.prior_attribution)
        retired_owned = {t: sid for t, sid in owners.items() if sid in retired}
        exits = [
            Order(
                client_id=make_id(sid, t, "sell" if portfolio.positions[t] > 0 else "cover"),
                ticker=t,
                side="sell" if portfolio.positions[t] > 0 else "buy",
                quantity=abs(portfolio.positions[t]),
                strategy_id=sid,
                position_effect="close",
            )
            for t, sid in sorted(retired_owned.items())
        ]
        if exits:
            log.info("tick.retired_exits", tickers=sorted(retired_owned))
    if pipeline.reason is not None and not exits:
        return noop(pipeline.reason)
    winner_id = pipeline.decided_by
    if winner_id is not None and not pipeline.exit_only:
        log.info(
            "tick.winner",
            strategy_id=winner_id,
            expected_return=pipeline.winner_return,
        )
    risk_adjustments = list(pipeline.adjustments)
    slice_policy = book_input.risk_overrides.get(winner_id) if winner_id else None
    proposed = pipeline.orders
    if exits:
        proposed = [o for o in proposed if o.ticker not in retired_owned] + exits
    outside: list[str] = []
    if settings.scoped:
        # the scope is the tick's tickers, even when the portfolio has its own
        # universe: the ranker scored only them (BE-03)
        allowed = set(settings.universe) & set(universe)
        outside = sorted({o.ticker for o in proposed if o.ticker not in allowed})
        if outside:
            log.info("tick.outside_universe_skipped", tickers=outside)
        proposed = [o for o in proposed if o.ticker in allowed]
    external_skipped: list[str] = []
    if external_holdings:
        proposed, external_skipped = drop_unowned_crossings(
            proposed, portfolio.positions, external_holdings
        )
        if external_skipped:
            log.warning("tick.external_holdings_skipped", tickers=external_skipped)

    halt = gate()
    if halt is not None:
        log.warning("tick.portfolio_halted", halt=halt.halt, gate=halt.gate, reason=halt.reason)
        # A halt keeps only position-reducing orders (sells of longs, covers).
        proposed = [] if halt.halt == "all" else [o for o in proposed if _reduces(o)]

    if broker is None:
        broker = _build_broker(
            portfolio, settings, prices, as_of, factory, book_prices.volumes, asset_classes
        )
        if book.spec.allow_short and isinstance(broker, SimulatedBroker):
            # Roadmap 16.1: a short book's paper broker trades on margin, with
            # the configured borrow lists and fees (BE-30).
            broker.enable_shorts(*short_account(book.spec.risk))
    # Roadmap 16.1: a short book's paper broker charges borrow fees and debit
    # interest for the days since the stored accrual date (else the last
    # snapshot), before any order changes the positions.
    financing: list[FinancingEvent] | None = None
    if book.spec.allow_short and isinstance(broker, SimulatedBroker) and not dry_run:
        financing = broker.accrue(as_of, since=last_accrual(state, portfolio_id) or since)
        for event in financing:
            log.info("tick.financing", ticker=event.ticker, kind=event.kind, amount=event.amount)
    # Every order records its decision: price, time, context and the
    # modelled cost (BL-32, P22).
    orders_with_tick = annotate_orders(
        [replace(o, tick_id=tick_id, portfolio_id=portfolio_id) for o in proposed],
        decided_at=datetime.combine(as_of, time(), UTC),
        prices=prices,
        signals=signals,
        cost_model=settings.cost_model,
        constructor=construction.method,
        exit_only=pipeline.exit_only,
        target_weights=pipeline.target_book.weights,
        volumes=book_prices.volumes,
        asset_classes=asset_classes,
    )
    open_conflicts: list[dict[str, str]] = []
    if external:
        orders_with_tick, open_conflicts = _drop_open_order_conflicts(
            state, orders_with_tick, log, portfolio_id=scope
        )
    sells = [o for o in orders_with_tick if o.side == "sell"]
    buys = [o for o in orders_with_tick if o.side == "buy"]

    placed = 0
    fills_count = 0
    any_failure = False
    # Simulated: broker outcomes are buffered and persisted together with the
    # portfolio snapshot in one transaction, so a crash can't leave fills
    # recorded without the snapshot that reflects them. External: see
    # ``place_external``.
    outcomes: list[tuple[Order, OrderStatus, Fill | None]] = []
    #: ``status_reason`` per client id (simulated partial fills).
    reasons: dict[str, str] = {}

    def place_external(order: Order) -> None:
        """The order row is committed as 'pending' *before* the broker sees
        the order, so no crash can leave a submitted order unrecorded: the
        next tick's ``reconcile_orders`` looks every pending row up by
        client id and books it, or rejects it when the broker never received
        it. Right after a submit the row is synced (broker id, status, fill)
        through the same ``reconcile_order``; fills are only ever booked from
        the broker's cumulative state, so never twice."""
        nonlocal placed, fills_count, any_failure
        with state.transaction():
            _record_order(state, order, status="pending", portfolio_id=scope)
        try:
            broker.place_order(order)
        except OrderRejectedError as exc:
            log.warning("tick.order.rejected", ticker=order.ticker, error=str(exc))
            _record_order(state, order, status="rejected", reason=str(exc), portfolio_id=scope)
            outcomes.append((order, "rejected", None))
            return
        except Exception as exc:
            # It may or may not have reached the broker: the row turns
            # ``unknown`` and nothing is sent for it again until the next
            # reconciliation settles it by client id (19.8).
            any_failure = True
            log.warning("tick.order.failed", ticker=order.ticker, error=str(exc))
            mark_unknown(state, order.client_id, f"submit outcome unknown: {exc}"[:500])
            return
        placed += 1
        outcomes.append((order, "pending", None))
        write_state(state, order.client_id, "submitted")
        try:
            assert isinstance(broker, OrderStateSource)  # checked when the broker opened
            synced = reconcile_order(broker, state, order.client_id, reject_unknown=False)
        except Exception as exc:
            log.warning("tick.order.sync_failed", client_id=order.client_id, error=str(exc))
            return
        fills_count += int(synced.fill_inserted)

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
            if external:
                place_external(order)
                continue
            try:
                fill = broker.place_order(order)
            except OrderRejectedError as exc:
                log.warning("tick.order.rejected", ticker=order.ticker, error=str(exc))
                outcomes.append((order, "rejected", None))
                continue
            except Exception as exc:
                any_failure = True
                log.warning("tick.order.failed", ticker=order.ticker, error=str(exc))
                continue
            placed += 1
            if fill is not None and fill.quantity < order.quantity - _QTY_EPSILON:
                # scaled down to cash (or volume): the row records what traded (TO-11)
                reasons[order.client_id] = (
                    f"filled {fill.quantity:g} of {order.quantity:g} requested"
                )
                order = replace(order, quantity=fill.quantity)
            outcomes.append((order, "filled" if fill else "rejected", fill))
            if fill is not None:
                fills_count += 1

    # 19.8: a live book decides now and submits later (order tickets) when
    # it has an approve subscription, when submit_in_window is on, or when
    # a runaway run or halt holds its closes for a person.
    runaway = external and (
        runaway_reason(risk_adjustments) is not None or _runaway_halted(run, book)
    )
    ticket_summary: dict[str, int] | None = None
    # 19.16: a short sale of a hard to borrow name waits for a person, even
    # in auto. With tickets off, the rest of the book is still sent at once.
    hard_to_borrow: frozenset[str] = frozenset()
    if external and not dry_run and tickets_recorded(state):
        hard_to_borrow = hard_to_borrow_orders(
            sells,
            _live_borrow(book, broker),
            as_of,
            fee_rate=run.settings.live.hard_to_borrow_fee_rate,
        )
        if hard_to_borrow:
            log.info("tick.hard_to_borrow_held", client_ids=sorted(hard_to_borrow))
    if external and not dry_run and _writes_tickets(run, book, runaway):
        if buys:  # checked against the pre-sell cash, as at the broker
            second = apply_book_risk(buys, book_input, market, slice_policy)
            risk_adjustments.extend(second.adjustments)
            buys = second.orders
        ticket_summary = _write_book_tickets(
            run, book, broker, [*sells, *buys], risk_adjustments, runaway, hard_to_borrow
        )
    else:
        if hard_to_borrow:
            held = [o for o in sells if o.client_id in hard_to_borrow]
            sells = [o for o in sells if o.client_id not in hard_to_borrow]
            ticket_summary = _write_book_tickets(
                run, book, broker, held, risk_adjustments, runaway, hard_to_borrow
            )
        # Sells first. The first risk pass counted their expected proceeds,
        # so buys are re-checked against the portfolio as it stands after
        # the sells: a rejected or unfilled sell must not fund a buy. (An
        # external broker's portfolio object is not updated by fills, so its
        # buys are checked against the pre-sell cash: conservative.)
        place(sells)
        if buys and not dry_run:
            second = apply_book_risk(buys, book_input, market, slice_policy)
            risk_adjustments.extend(second.adjustments)
            buys = second.orders
        place(buys)

    def hooks(after: Portfolio, marks: Mapping[str, float]) -> dict[str, Any]:
        return run_portfolio_hooks(
            PortfolioHookContext(
                state=state,
                tick_id=tick_id,
                as_of=as_of,
                portfolio_id=portfolio_id,
                portfolio=after,
                prices=marks,
                pipeline=pipeline,
                outcomes=outcomes,
                subscription_ids=book.subscription_ids,
                scoped=run.scoped,
            ),
            log,
        )

    hook_summary: dict[str, Any] = {}
    after = portfolio
    if not dry_run and external:
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
            persist_corporate_actions()
            _snapshot_portfolio(state, tick_id, after, marks, as_of, portfolio_id=scope)
            hook_summary = hooks(after, marks)
    elif not dry_run:
        with state.transaction():
            for order, order_status, fill in outcomes:
                _record_order(
                    state,
                    order,
                    status=order_status,
                    reason=reasons.get(order.client_id or ""),
                    portfolio_id=scope,
                )
                if fill is not None:
                    _record_fill(
                        state, fill, portfolio_id=scope, arrival_price=_arrival(broker, fill)
                    )
            persist_corporate_actions()
            if financing is not None:
                record_accrual(state, portfolio_id, as_of, financing, tick_id=tick_id)
            whole = merge_holdings(portfolio, manual_holdings) if manual_holdings else portfolio
            _snapshot_portfolio(state, tick_id, whole, prices, as_of, portfolio_id=scope)
            hook_summary = hooks(whole, prices)

    rejected = [order.ticker for order, st, _ in outcomes if st == "rejected"]
    if rejected:
        fields: dict[str, Any] = {
            "tick_id": tick_id,
            "as_of": as_of.isoformat(),
            "rejected": rejected,
        }
        if not book.legacy:
            fields["portfolio_id"] = portfolio_id
        _safe_notify(
            run.notifier,
            Notification(
                level="warning",
                title="orders rejected",
                message=f"{len(rejected)} order(s) rejected by the broker",
                fields=fields,
            ),
            log,
        )

    ended: list[str] = []
    if retired and run.scoped and not dry_run:
        holdings = (after if external else portfolio).positions
        ended = _end_retired_subscriptions(run, book, retired, retired_owned, holdings)
    exit_strategy_id = winner_id if pipeline.exit_only else None
    status: TickStatus = "ok" if not any_failure else "partial"
    return result(
        status,
        None if exit_strategy_id else winner_id,
        placed,
        fills_count,
        {
            "winner_strategy_id": None if exit_strategy_id else winner_id,
            "winner_expected_return": pipeline.winner_return,
            **(
                {"reason": "no_candidates", "exit_strategy_id": exit_strategy_id}
                if exit_strategy_id
                else {}
            ),
            "stale_buys_dropped": pipeline.stale_buys,
            **({"outside_universe_skipped": outside} if outside else {}),
            **({"external_holdings_skipped": external_skipped} if external_skipped else {}),
            **({"retired_exits": sorted(retired_owned)} if retired_owned else {}),
            **({"retired_subscriptions_ended": ended} if ended else {}),
            **({"open_order_conflicts": open_conflicts} if open_conflicts else {}),
            **({"tickets": ticket_summary} if ticket_summary is not None else {}),
            **({"hard_to_borrow": sorted(hard_to_borrow)} if hard_to_borrow else {}),
            **halted(halt),
            **({"constructor": construction.method} if not book.legacy else {}),
            "orders_placed": placed,
            "fills": fills_count,
            "risk_adjustments": [a.as_dict() for a in risk_adjustments],
            **corporate_summary,
            **({"financing": round(sum(e.amount for e in financing), 6)} if financing else {}),
            **hook_summary,
        },
    )


# ---- live books: context, the reconciliation gate and tickets (19.8) ------------------


def _live_context(
    run: _TickRun,
    book: TickBook,
    broker: Broker | None,
    tickers: Sequence[str],
    external_positions: Mapping[str, float],
) -> LiveContext:
    """What the live safeguards see of ``book`` (never raises: a context
    that cannot be built is empty, and the rules then refuse to open)."""
    from stonks.production.live.context import build_live_context
    from stonks.production.rules._common import settings_of

    try:
        return build_live_context(
            run.state,
            book.portfolio_id,
            run.as_of,
            broker=broker,
            tickers=tickers,
            lake=run.lake,
            account_settings=settings_of(book.spec.risk, "account_rules"),
            external_positions=external_positions,
        )
    except Exception as exc:
        run.log.error("tick.live_context_failed", portfolio_id=book.portfolio_id, error=str(exc))
        return LiveContext(portfolio_id=book.portfolio_id)


def _unreconciled(run: _TickRun, book: TickBook, unresolved: Sequence[str]) -> BookResult:
    """The startup reconciliation gate is shut: orders of the book are still
    ``unknown`` at the broker, so the book decides nothing this run."""
    run.log.warning(
        "tick.orders_unreconciled", portfolio_id=book.portfolio_id, client_ids=list(unresolved)
    )
    _safe_notify(
        run.notifier,
        Notification(
            level="warning",
            title="orders not reconciled",
            message=(
                f"{len(unresolved)} order(s) in an unknown state at the broker;"
                " the book waits for reconciliation"
            ),
            fields={
                "tick_id": run.tick_id,
                "portfolio_id": book.portfolio_id,
                "client_ids": list(unresolved)[:10],
            },
        ),
        run.log,
    )
    return BookResult(
        portfolio_id=book.portfolio_id,
        status="noop",
        winner_strategy_id=None,
        orders_placed=0,
        fills=0,
        summary={"reason": "orders_unreconciled", "unknown_orders": list(unresolved)},
    )


def _approve_strategies(book: TickBook) -> set[str]:
    return {s for s, m in book.spec.strategy_modes.items() if m is Mode.APPROVE}


def _runaway_halted(run: _TickRun, book: TickBook) -> bool:
    """A ``runaway`` halt is in force for the book: whatever it lets through
    waits for a person."""
    return any(
        h.kind == "runaway"
        for h in active_halts(run.state, run.as_of, portfolio_id=book.portfolio_id)
    )


def _writes_tickets(run: _TickRun, book: TickBook, runaway: bool) -> bool:
    if not tickets_recorded(run.state):
        return False
    return run.settings.live.submit_in_window or runaway or bool(_approve_strategies(book))


def _write_book_tickets(
    run: _TickRun,
    book: TickBook,
    broker: Broker | None,
    orders: Sequence[Order],
    adjustments: Sequence[RiskAdjustment],
    runaway: bool,
    hard_to_borrow: Collection[str] = (),
) -> dict[str, int]:
    """One ticket per order (an order already in the ledger is left out),
    with the rules that touched it and the broker's what-if answer. The
    owner hears when tickets wait for them."""
    state = run.state
    fresh = [o for o in orders if _live_order_status(state, o.client_id) is None]
    approve = _approve_strategies(book)
    auto = {s for s, m in book.spec.strategy_modes.items() if m is Mode.AUTO}
    rules = {o.client_id: [a.as_dict() for a in adjustments if a.ticker == o.ticker] for o in fresh}
    written = write_tickets(
        state,
        fresh,
        portfolio_id=book.portfolio_id,
        tick_id=run.tick_id,
        as_of=run.as_of,
        window=submit_window(run.as_of, run.settings.live.submit),
        hold=lambda o: ticket_hold(
            o,
            approve_strategies=approve,
            auto_strategies=auto,
            runaway=runaway,
            hard_to_borrow=o.client_id in hard_to_borrow,
        ),
        now=datetime.now(UTC),
        rules=rules,
        previews=_what_if(broker, fresh, run.log),
    )
    awaiting = sum(t.awaiting_approval for t in written)
    run.log.info("tick.tickets_written", written=len(written), awaiting_approval=awaiting)
    if awaiting:
        _notify_awaiting(run, book.portfolio_id, awaiting)
    return {
        "written": len(written),
        "awaiting_approval": awaiting,
        "approved": len(written) - awaiting,
    }


def _live_borrow(book: TickBook, broker: Broker | None) -> BorrowSource | None:
    """The borrow source a live book's short sales are checked against: the
    broker's own (``IbkrBroker.borrow``, today's locate), else the book's
    configured one (``borrow_check``, then ``squeeze_guard``)."""
    own = getattr(broker, "borrow", None)
    if isinstance(own, BorrowSource):
        return own
    return short_account(book.spec.risk)[1]


def _what_if(broker: Broker | None, orders: Sequence[Order], log: Any) -> dict[str, dict[str, Any]]:
    """The broker's what-if answer per order, when it can preview one. A
    failed preview is noted on the ticket, never fatal."""
    if broker is None or not isinstance(broker, MarginPreviewer):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for order in orders:
        try:
            preview = broker.what_if(order)
        except Exception as exc:
            log.warning("tick.what_if_failed", client_id=order.client_id, error=str(exc))
            out[order.client_id] = {"what_if_error": str(exc)[:200]}
            continue
        out[order.client_id] = {
            "what_if": {
                "commission": preview.commission,
                "commission_currency": preview.commission_currency,
                "initial_margin_change": preview.initial_margin_change,
                "maintenance_margin_change": preview.maintenance_margin_change,
                "equity_with_loan_after": preview.equity_with_loan_after,
                "warning": preview.warning,
            }
        }
    return out


def _notify_awaiting(run: _TickRun, portfolio_id: str, count: int) -> None:
    """A high-urgency push to the owner: how many orders wait, and where.
    No amounts or tickers leave the server."""
    from stonks.notify.events import Audience, Event
    from stonks.notify.router import configured_router

    rows = run.state.sql("SELECT name FROM portfolios WHERE id = ?", [portfolio_id])
    name = rows[0]["name"] if rows else portfolio_id
    noun = "order waits" if count == 1 else "orders wait"
    try:
        configured_router(run.state).publish(
            Event(
                category="order",
                level="warning",
                urgency="high",
                title="Orders wait for your approval",
                body=f"{count} {noun} for approval in {name}",
                audience=Audience.owner_of(portfolio_id),
                dedupe_key=f"tickets:{portfolio_id}:{run.as_of.isoformat()}",
                deep_link="/tickets",
                portfolio_id=portfolio_id,
            )
        )
    except Exception as exc:
        run.log.error("tick.tickets_notify_failed", portfolio_id=portfolio_id, error=str(exc))


# ---- interrupted ticks ----------------------------------------------------------------

#: ``summary_json.error`` of a tick row closed by :func:`recover_interrupted_ticks`.
INTERRUPTED_ERROR = "interrupted: the process running the tick stopped before it finished"

#: ``summary_json`` key of a running tick: the host and process running it.
OWNER_KEY = "owner"
#: A running tick owned by another host is taken for dead after this long.
FOREIGN_TICK_MAX_AGE = timedelta(hours=12)
#: Ticks running in this process right now.
_RUNNING: set[str] = set()


def tick_owner() -> dict[str, Any]:
    """The host and process that run a tick (written on its running row)."""
    return {"host": socket.gethostname(), "pid": os.getpid()}


def _process_alive(pid: int) -> bool:
    """Whether a process with ``pid`` runs on this host."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _owner_gone(tick_id: str, summary: str | None, started_at: str, now: datetime) -> bool:
    """A running row may be closed: it names no owner (written before
    owners were recorded), its owner process on this host is gone, or its
    owner is another host and the row is older than
    :data:`FOREIGN_TICK_MAX_AGE` (BE-44)."""
    try:
        owner = json.loads(summary).get(OWNER_KEY) if summary else None
    except (ValueError, AttributeError):
        owner = None
    if not isinstance(owner, dict):
        return True
    if owner.get("host") == socket.gethostname():
        pid = int(owner.get("pid") or 0)
        if pid == os.getpid():  # this process: gone unless it runs it now
            return tick_id not in _RUNNING
        return not _process_alive(pid)
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return now - started > FOREIGN_TICK_MAX_AGE


def recover_interrupted_ticks(state: SqliteState, *, now: datetime | None = None) -> list[str]:
    """Close every ``tick_runs`` row still ``running`` whose process is gone
    as ``error`` (TO-06, BE-44).

    Call at the start of the process that runs ticks (the API, whose tick
    jobs never outlive it, or the ``local`` scheduler). A running row names
    the host and process that run it: a row of a live process (a CLI tick
    running right now) is left alone, and so is a young row of another
    host. A killed tick committed nothing past its last transaction, so a
    same-day rerun repeats it safely (idempotent client ids). Without this
    the stuck-tick health check fails for ever and the operational halt
    blocks every buy. Returns the closed ids."""
    clock = now or datetime.now(UTC)
    at = clock.isoformat(timespec="seconds")
    rows = state.sql(
        "SELECT id, started_at, summary_json FROM tick_runs WHERE status = 'running'"
        " ORDER BY started_at"
    )
    ids = [r["id"] for r in rows if _owner_gone(r["id"], r["summary_json"], r["started_at"], clock)]
    if not ids:
        return []
    summary = json.dumps({"error": INTERRUPTED_ERROR, "error_type": "Interrupted"})
    with state.transaction():
        for tick_id in ids:
            state.execute(
                "UPDATE tick_runs SET status = 'error', finished_at = ?, summary_json = ?"
                " WHERE id = ? AND status = 'running'",
                [at, summary, tick_id],
            )
    _log.warning("tick.recovered_interrupted", tick_ids=ids)
    return ids


# ---- plans from portfolios and subscriptions -----------------------------------------


def load_tick_plan(
    state: SqliteState,
    settings: TickSettings,
    traders: TraderFactory | None = None,
    *,
    dry_run: bool = False,
) -> TickPlan:
    """The books of every active portfolio (of an active owner), plus every
    enabled notify-mode subscription (design section 5, S6):

    - a simulated portfolio: one ``paper`` book over its paper subscriptions
      (auto ones are left out: the auto gate refuses them, this is the
      tick's own guard);
    - a portfolio that trades at a broker (``kind = broker``, or the default
      portfolio of an install whose ``[brokers].kind`` is external): an
      ``auto`` book over its running auto subscriptions, traded through its
      connection (``traders``), and a ``paper`` book over its paper
      subscriptions on its simulated paper account
      (:func:`~stonks.accounts.paper.ensure_paper_account`), so paper money
      never reaches the real account.

    The simulated default portfolio keeps a book while it holds positions,
    even with no strategy subscribed, so corporate actions on its holdings
    still apply. Every book is tightened by its owner's risk limits.

    At an external broker the system's paper ``pf_default`` rows turn auto
    first (:func:`~stonks.accounts.default_book.align_default_book`, BE-10),
    and a live default that holds positions with no auto strategy is named
    in ``notices``. A dry run writes nothing (no paper account rows, no
    mode switch, BE-52)."""
    live_default = settings.broker_kind != "simulated"
    if live_default and not dry_run:
        align_default_book(state, settings.broker_kind)
    rows = state.sql(
        "SELECT p.*, u.risk_policy_json AS owner_risk_json FROM portfolios p"
        " JOIN users u ON u.id = p.owner_id WHERE p.status = 'active' AND u.status = 'active'"
        " AND p.paper_of IS NULL ORDER BY p.created_at, p.id"
    )
    subs = [
        Subscription.from_row(r)
        for r in state.sql(
            "SELECT s.* FROM subscriptions s JOIN users u ON u.id = s.user_id"
            " WHERE s.enabled = 1 AND u.status = 'active' ORDER BY s.created_at, s.id"
        )
    ]
    global_construction = _construction_mapping(settings.construction)
    books: list[TickBook] = []

    def add(
        portfolio: AccountPortfolio,
        own: list[Subscription],
        owner_risk: Mapping[str, Any],
        mode: Literal["paper", "auto"],
        *,
        keep_holdings: bool = False,
    ) -> None:
        spec = BookSpec.for_portfolio(
            portfolio,
            own,
            global_risk=settings.risk,
            global_construction=global_construction,
            default_initial_cash=settings.initial_cash,
            owner_risk=owner_risk or None,
        )
        if not spec.strategy_weights and not (keep_holdings and holds_positions(portfolio.id)):
            return
        account: AccountPortfolio | None = portfolio
        parent: str | None = None
        if mode == "paper" and trades_at_broker(portfolio):
            paper = ensure_paper_account(state, portfolio, create=not dry_run)
            spec = replace(spec, portfolio_id=paper.id, broker="simulated")
            account, parent = paper, portfolio.id
        books.append(
            TickBook(
                spec=spec,
                owner_id=portfolio.owner_id,
                subscription_ids={
                    s.strategy_id: s.id
                    for s in own
                    if s.strategy_id in (spec.strategy_weights or {})
                },
                mode=mode,
                account=account,
                parent_id=parent,
            )
        )

    def trades_at_broker(portfolio: AccountPortfolio) -> bool:
        return portfolio.kind == "broker" or (live_default and portfolio.id == DEFAULT_PORTFOLIO_ID)

    def holds_positions(portfolio_id: str) -> bool:
        return bool(_load_or_seed_portfolio(state, 0.0, portfolio_id=portfolio_id).positions)

    for row in rows:
        portfolio = AccountPortfolio.from_row(row)
        owner_risk = json.loads(row["owner_risk_json"] or "{}")
        own = [s for s in subs if s.portfolio_id == portfolio.id]
        if trades_at_broker(portfolio):
            # approve and auto subscriptions share the broker account's book
            add(portfolio, [s for s in own if s.mode.trades_live], owner_risk, "auto")
        # The simulated default portfolio keeps its book while it holds
        # positions, so corporate actions apply with no strategy subscribed.
        keep = portfolio.id == DEFAULT_PORTFOLIO_ID and not trades_at_broker(portfolio)
        paper = [s for s in own if s.mode is Mode.PAPER]
        add(portfolio, paper, owner_risk, "paper", keep_holdings=keep)
    notify = tuple(s for s in subs if s.mode is Mode.NOTIFY)
    notices: list[str] = []
    auto_default = any(b.portfolio_id == DEFAULT_PORTFOLIO_ID and b.mode == "auto" for b in books)
    if live_default and not auto_default and holds_positions(DEFAULT_PORTFOLIO_ID):
        notices.append(
            f"{DEFAULT_PORTFOLIO_ID} trades at {settings.broker_kind} and holds positions,"
            " but no auto subscription runs on it: nothing manages those holdings"
        )
    return TickPlan(books=tuple(books), notify=notify, traders=traders, notices=tuple(notices))


def _pause_on_broker_error(
    run: _TickRun, book: TickBook, outcome: BookResult, error: BaseException | str
) -> BookResult:
    """An auto book whose broker failed pauses its auto subscriptions. A
    short outage only skips the day (roadmap 19.5): a long one pauses
    through the broker health job and the reconciliation checks."""
    if book.mode != "auto" or run.dry_run or not run.scoped:
        return outcome
    if is_short_outage(error):
        run.log.warning("tick.broker_outage", portfolio_id=book.portfolio_id, error=str(error))
        return replace(outcome, summary={**outcome.summary, "broker_outage": True})
    reason = broker_error_reason(error)
    try:
        paused = pause_auto(
            run.state,
            book.portfolio_id,
            book.subscriptions_in(Mode.AUTO),
            reason,
            tick_id=run.tick_id,
            as_of=run.as_of,
        )
    except Exception as exc:  # pragma: no cover - best effort, the tick goes on
        run.log.error("tick.auto_pause_failed", portfolio_id=book.portfolio_id, error=str(exc))
        return outcome
    if not paused:
        return outcome
    return replace(outcome, summary={**outcome.summary, "auto_paused": paused})


def _holding_owners(
    state: SqliteState,
    portfolio_id: str,
    held: Sequence[str],
    prior: Mapping[str, Mapping[str, float]],
) -> dict[str, str]:
    """The strategy that owns each held ticker: the largest share of its
    last attribution, else the strategy behind its latest fill here."""
    owners: dict[str, str] = {}
    for ticker in held:
        shares: Mapping[str, float] = prior.get(ticker) or {}
        if shares:
            owners[ticker] = min(shares, key=lambda sid, s=shares: (-abs(s[sid]), sid))
    rest = [t for t in held if t not in owners]
    if rest:
        marks = ",".join("?" for _ in rest)
        rows = state.sql(
            "SELECT ticker, strategy_id FROM orders WHERE portfolio_id = ? AND strategy_id IS NOT"
            f" NULL AND status IN ('filled', 'partially_filled') AND ticker IN ({marks})"
            " ORDER BY updated_at DESC, created_at DESC, rowid DESC",
            [portfolio_id, *rest],
        )
        for r in rows:
            owners.setdefault(r["ticker"], r["strategy_id"])
    return owners


def _end_retired_subscriptions(
    run: _TickRun,
    book: TickBook,
    retired: Sequence[str],
    owned: Mapping[str, str],
    holdings: Mapping[str, float],
) -> list[str]:
    """Disable the book's subscriptions of retired strategies that hold
    nothing any more (BE-18; design: exit-only until flat, then disable).
    Audited as ``service:system``. Never raises."""
    from stonks.accounts.scope import Scope
    from stonks.accounts.subscriptions import SubscriptionRepository

    still = {sid for t, sid in owned.items() if abs(holdings.get(t, 0.0)) > _QTY_EPSILON}
    ended: list[str] = []
    repo = SubscriptionRepository(run.state)
    system = Scope.service("system")
    for sid in retired:
        sub_id = book.subscription_ids.get(sid)
        if sid in still or sub_id is None:
            continue
        try:
            repo.disable(system, sub_id, reason="strategy retired and its holdings are closed")
        except Exception as exc:  # pragma: no cover - best effort
            run.log.error("tick.retired_disable_failed", subscription_id=sub_id, error=str(exc))
            continue
        ended.append(sub_id)
    return ended


def _pause_inactive_auto(run: _TickRun, book: TickBook, outcome: BookResult) -> BookResult:
    """Pause the book's auto subscriptions whose strategy is not active
    (BE-01): the book already left them out, this makes it stick."""
    if book.mode != "auto" or run.dry_run or not run.scoped:
        return outcome
    by_strategy: dict[str, list[str]] = {}
    for sid in inactive_auto(book, run.active):
        if sid in book.subscription_ids:
            by_strategy.setdefault(sid, []).append(book.subscription_ids[sid])
    if not by_strategy:
        return outcome
    paused: list[str] = []
    for sid, ids in by_strategy.items():
        status = run.statuses.get(sid, "missing")
        try:
            paused += pause_auto(
                run.state,
                book.portfolio_id,
                ids,
                strategy_not_active_reason(status),
                tick_id=run.tick_id,
                as_of=run.as_of,
            )
        except Exception as exc:  # pragma: no cover - best effort, the tick goes on
            run.log.error("tick.auto_pause_failed", portfolio_id=book.portfolio_id, error=str(exc))
    if not paused:
        return outcome
    prior = list(outcome.summary.get("auto_paused") or [])
    return replace(outcome, summary={**outcome.summary, "auto_paused": [*prior, *paused]})


def _record_portfolio_run(
    run: _TickRun, book: TickBook, outcome: BookResult, started_at: str
) -> None:
    """One ``portfolio_runs`` row per book. Never raises."""
    state = run.state
    if run.dry_run or not run.scoped:
        return
    try:
        if not runs_recorded(state):
            return
        halted = outcome.summary.get("halted") or {}
        breached = any(
            h.kind in _BREACH_KINDS and h.scope == "portfolio"
            for h in active_halts(state, run.as_of, portfolio_id=book.portfolio_id)
        )
        rejected = state.sql(
            "SELECT COUNT(*) FROM orders WHERE tick_id = ? AND portfolio_id = ?"
            " AND status = 'rejected'",
            [run.tick_id, book.portfolio_id],
        )[0][0]
        status = "paused" if outcome.summary.get("auto_paused") else outcome.status
        error = outcome.summary.get("error")
        record_run(
            state,
            PortfolioRun(
                tick_id=run.tick_id,
                portfolio_id=book.portfolio_id,
                as_of=run.as_of,
                mode=book.run_mode,
                status=status,
                started_at=started_at,
                finished_at=_iso_now(),
                orders_placed=outcome.orders_placed,
                fills=outcome.fills,
                orders_rejected=int(rejected),
                risk_breached=breached,
                halted=halted.get("halt"),
                error=f"{outcome.summary.get('error_type', 'Error')}: {error}" if error else None,
                paper_subscriptions=book.subscriptions_in(Mode.PAPER),
                auto_subscriptions=sorted(
                    book.subscriptions_in(Mode.AUTO) + book.subscriptions_in(Mode.APPROVE)
                ),
            ),
        )
    except Exception as exc:
        run.log.error("tick.portfolio_run_failed", portfolio_id=book.portfolio_id, error=str(exc))


def _construction_mapping(construction: ConstructionSettings) -> dict[str, Any]:
    """The flat mapping :func:`stonks.accounts.book.merge_construction` merges."""
    flat = construction.model_dump(exclude={"params"})
    flat.update(construction.params)
    return {k: v for k, v in flat.items() if v is not None}


def _notify_signals(run: _TickRun) -> list[NotifySignal]:
    if not run.plan.notify:
        return []
    signals = run.all_signals() if _needs_shadow_signals(run) else run.signals
    out: list[NotifySignal] = []
    for sub in run.plan.notify:
        # a strategy scored today with no picks still counts: that is the
        # day its exits are written (BE-16)
        if sub.strategy_id not in signals.scores:
            continue
        scores = signals.scores[sub.strategy_id]
        picks = sorted(scores.items(), key=lambda p: p[1], reverse=True)
        out.append(
            NotifySignal(
                subscription_id=sub.id,
                user_id=sub.user_id,
                strategy_id=sub.strategy_id,
                portfolio_id=sub.portfolio_id,
                picks=tuple(picks),
            )
        )
    return out


# ---- helpers ---------------------------------------------------------------


def _reduces(order: Order) -> bool:
    """A sell of a long or a cover of a short (never a short sale)."""
    if order.position_effect is not None:
        return order.position_effect == "close"
    return order.side == "sell"


def _client_id_fn(as_of: date, portfolio_id: str) -> Callable[[str | None, str, SideToken], str]:
    """Deterministic client ids (:func:`make_client_id`: the default
    portfolio keeps the pre-accounts format, others carry their id)."""

    def make(strategy_id: str | None, ticker: str, side: SideToken) -> str:
        return make_client_id(
            as_of=as_of,
            strategy_id=strategy_id or PORTFOLIO_STRATEGY,
            ticker=ticker,
            side=side,
            portfolio_id=portfolio_id,
        )

    return make


def _buyable(book: PriceBook, due: Mapping[str, date] | None) -> frozenset[str]:
    """Fresh tickers, less those whose due session bar is missing."""
    if not due:
        return book.fresh
    return frozenset(
        t for t in book.fresh if t not in due or book.bar_dates.get(t, date.min) >= due[t]
    )


def _in_universe(scores: Mapping[str, float], universe: Sequence[str] | None) -> dict[str, float]:
    if universe is None:
        return dict(scores)
    allowed = set(universe)
    return {t: r for t, r in scores.items() if t in allowed}


def _build_broker(
    portfolio: Portfolio,
    settings: TickSettings,
    prices: dict[str, float],
    as_of: date,
    factory: BrokerFactory | None = None,
    volumes: dict[str, float] | None = None,
    asset_classes: dict[str, str] | None = None,
) -> Broker:
    """The one place the tick constructs its broker around ``portfolio``.

    A simulated broker fills at the latest close through
    ``settings.simulated_costs`` and gets what that cost model reads, as in
    backtests: each ticker's asset class and the volume of the priced bar."""
    if factory is not None:
        broker = factory(portfolio)
    else:
        broker = settings.simulated_costs.build_broker(portfolio)
    if isinstance(broker, SimulatedBroker):
        broker.set_asset_classes(asset_classes or {})  # type: ignore[arg-type]
        broker.set_prices(prices, as_of=as_of, volumes=volumes)
    return broker


def _shadow_phase(run: _TickRun) -> dict[str, Any]:
    """Advance the model books; return the tick-summary fragment. Never
    raises: a model-book failure must not fail the real tick."""
    settings, lake, state, log = run.settings, run.lake, run.state, run.log
    if run.dry_run or not settings.shadow_enabled:
        return {}
    try:
        shadow = run.shadow_signals()
        statuses: tuple[str, ...] = ("shadow",)
        if settings.model_books == "all":
            shadow = run.signals.merged(shadow)
            statuses = ("active", "shadow")
        # Shadow portfolios may hold tickers outside the universe too.
        version_books = active_version_books(state, run.registry)
        shadow_held = sorted(
            {*shadow_held_tickers(state), *version_held_tickers(state, version_books)}
        )
        shadow_actions = load_corporate_actions(lake, shadow_held)
        book = load_prices(
            lake,
            settings.universe,
            shadow_held,
            run.as_of,
            max_staleness_days=settings.max_price_staleness_days,
        )
        shadow_prices = book.prices
        base_context = None
        if needs_risk_context(settings.risk):
            base_context = build_risk_context(
                lake,
                state,
                Portfolio(cash=0.0),
                shadow_prices,
                run.as_of,
                policy=settings.risk,
                universe=[*settings.universe, *shadow_held],
                cost_model=settings.costs,
                volumes=book.volumes,
            )
        outcomes = evaluate_shadow_strategies(
            state,
            run.registry,
            shadow.ranked(),
            book.prices,
            _asset_classes(lake, [*settings.universe, *shadow_held]),
            run.as_of,
            run.tick_id,
            settings,
            buyable=book.fresh,
            volumes=book.volumes,
            corporate_actions=shadow_actions,
            strategies=run.pool.checkout,
            statuses=statuses,
            risk_context=base_context,
        )
    except Exception as exc:
        log.error("tick.shadow_failed", error=str(exc), error_type=type(exc).__name__)
        return {"shadow_error": f"{type(exc).__name__}: {exc}"}
    summary: dict[str, Any] = {"shadow": [o.as_dict() for o in outcomes]}
    if version_books:
        summary.update(
            _version_book_phase(run, version_books, book, shadow_held, shadow_actions, base_context)
        )
    return summary


def _version_book_phase(
    run: _TickRun,
    books: Sequence[VersionBook],
    book: Any,
    held: Sequence[str],
    actions: Any,
    base_context: Any,
) -> dict[str, Any]:
    """Advance the model version books (roadmap 22.6): each candidate model
    and the live one it competes with. Never raises."""
    settings, state = run.settings, run.state
    try:
        loaders = {b.store.book_id: b.load for b in books}
        signals = Ranker(
            registry=run.registry,
            lake=run.lake,
            universe=settings.universe,
            threshold=settings.threshold,
            universe_id=settings.universe_id,
            loaders=loaders,
        ).score(as_of=run.as_of)

        def strategy(bid: str) -> Strategy:
            found = signals.instances.get(bid)
            return found if found is not None else loaders[bid]()

        outcomes = evaluate_books(
            state,
            [b.store for b in books],
            signals.ranked(),
            book.prices,
            _asset_classes(run.lake, [*settings.universe, *held]),
            run.as_of,
            run.tick_id,
            settings,
            strategies=strategy,
            buyable=book.fresh,
            volumes=book.volumes,
            corporate_actions=actions,
            risk_context=base_context,
        )
    except Exception as exc:
        run.log.error("tick.version_books_failed", error=str(exc), error_type=type(exc).__name__)
        return {"model_versions_error": f"{type(exc).__name__}: {exc}"}
    return {"model_versions": [o.as_dict() for o in outcomes]}


def _record_signal_phase(run: _TickRun) -> None:
    """Store the day's signals and events (``production.signals``) of every
    strategy scored this tick, after the model books advanced. Never
    raises: a failure here must not fail the tick."""
    if run.dry_run:
        return
    try:
        if not signals_recorded(run.state):
            return
        scored = run.signals if run._shadow is None else run.signals.merged(run._shadow)
        record_signals(
            run.state,
            run.lake,
            scored.scores,
            scored.instances,
            tick_id=run.tick_id,
            as_of=run.as_of,
            max_price_staleness_days=run.settings.max_price_staleness_days,
        )
    except Exception as exc:
        run.log.error("tick.signals_failed", error=str(exc), error_type=type(exc).__name__)


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


def utc_today(clock: Callable[[], datetime] | None = None) -> date:
    """Today's date in UTC, the calendar all stored timestamps use.
    ``clock`` returns an aware datetime (default: the system clock)."""
    return (clock() if clock is not None else datetime.now(UTC)).astimezone(UTC).date()


def _new_tick_id(as_of: date) -> str:
    return f"tick_{as_of.isoformat()}_{uuid.uuid4().hex[:8]}"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _ledger_scoped(state: SqliteState) -> bool:
    """The ledger tables carry ``portfolio_id`` (accounts schema, 010)."""
    cols = {r["name"] for r in state.sql("SELECT name FROM pragma_table_info('orders')")}
    return "portfolio_id" in cols


def _scoped(portfolio_id: str | None, sql: str, params: list[Any]) -> tuple[str, list[Any]]:
    """``sql`` (ending in a WHERE clause or none) narrowed to one portfolio."""
    if portfolio_id is None:
        return sql, params
    joiner = " AND " if " WHERE " in sql else " WHERE "
    return f"{sql}{joiner}portfolio_id = ?", [*params, portfolio_id]


def _refuse_backdated(
    state: SqliteState, as_of: date, log: Any, portfolio_ids: Sequence[str]
) -> None:
    """Refuse when any traded portfolio's own ledger (tick snapshots; a
    broker sync's rows are not the tick's) is already past ``as_of``."""
    for portfolio_id in portfolio_ids:
        latest = _latest_snapshot_as_of(state, portfolio_id=portfolio_id)
        if latest is not None and as_of < latest:
            log.error(
                "tick.backdated_refused",
                latest_snapshot_as_of=latest.isoformat(),
                portfolio_id=portfolio_id,
            )
            raise BackdatedTickError(
                f"refusing to trade as_of {as_of.isoformat()}: the portfolio already has a "
                f"snapshot for {latest.isoformat()}; run with --dry-run to inspect a past date"
            )


def _latest_snapshot_as_of(state: SqliteState, portfolio_id: str | None = None) -> date | None:
    """``as_of`` of the portfolio's latest dated tick snapshot (``None``: none)."""
    where, params = ledger_filter(state, "portfolio_snapshots", portfolio_id, tick_only=True)
    latest = state.sql(
        f"SELECT MAX(as_of) AS as_of FROM portfolio_snapshots WHERE {where}", params
    )[0]["as_of"]
    return date.fromisoformat(latest) if latest else None


def _load_or_seed_portfolio(
    state: SqliteState, initial_cash: float, portfolio_id: str | None = None
) -> Portfolio:
    # NULL as_of (rows written without one) sorts last under DESC.
    where, params = ledger_filter(state, "portfolio_snapshots", portfolio_id, tick_only=True)
    rows = state.sql(
        f"SELECT cash, positions_json FROM portfolio_snapshots WHERE {where}"
        " ORDER BY as_of DESC, id DESC LIMIT 1",
        params,
    )
    if not rows:
        return Portfolio(cash=initial_cash, positions={})
    row = rows[0]
    return Portfolio(cash=float(row["cash"]), positions=json.loads(row["positions_json"]))


def _working_orders(state: SqliteState, portfolio_id: str | None) -> dict[str, str]:
    """Orders still working at the broker, of this portfolio only, so a
    split adjusts each portfolio's orders exactly once."""
    working = working_orders(state)
    if portfolio_id is None or not working:
        return working
    marks = ",".join("?" for _ in working)
    rows = state.sql(
        f"SELECT client_id FROM orders WHERE portfolio_id = ? AND client_id IN ({marks})",
        [portfolio_id, *working],
    )
    mine = {r["client_id"] for r in rows}
    return {cid: t for cid, t in working.items() if cid in mine}


def _position_owner(
    state: SqliteState,
    registry: StrategyRegistry,
    held: Sequence[str],
    log: Any,
    portfolio_id: str | None = None,
    candidates: set[str] | None = None,
) -> str | None:
    """The active strategy behind the most recent fill on a held ticker of
    this portfolio, or None when every strategy that bought the holdings is
    no longer active (or, for a subscription book, no longer in it)."""
    placeholders = ",".join("?" for _ in held)
    sql, params = _scoped(
        portfolio_id,
        "SELECT strategy_id FROM orders"
        " WHERE status IN ('filled', 'partially_filled') AND strategy_id IS NOT NULL"
        f" AND ticker IN ({placeholders})",
        list(held),
    )
    rows = state.sql(f"{sql} ORDER BY updated_at DESC, created_at DESC, rowid DESC", params)
    allowed = {h.id for h in registry.list_all(status="active")}
    if candidates is not None:
        shadow = {h.id for h in registry.list_all(status="shadow")}
        allowed = (allowed | shadow) & candidates
    for row in rows:
        if row["strategy_id"] in allowed:
            return row["strategy_id"]
    log.warning(
        "tick.exit.no_active_owner",
        held=list(held),
        owners=sorted({r["strategy_id"] for r in rows}),
    )
    return None


def _drop_open_order_conflicts(
    state: SqliteState, orders: list[Order], log: Any, portfolio_id: str | None = None
) -> tuple[list[Order], list[dict[str, str]]]:
    """Drop orders on a (ticker, side) that already has a working order at
    the broker under another client_id (e.g. yesterday's GTC buy still
    open): the portfolio doesn't show it yet, so deciding again would double
    the position once both fill."""
    placeholders = ",".join("?" for _ in NON_TERMINAL_STATUSES)
    sql, params = _scoped(
        portfolio_id,
        f"SELECT client_id, ticker, side FROM orders WHERE status IN ({placeholders})",
        list(NON_TERMINAL_STATUSES),
    )
    open_rows = state.sql(sql, params)
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


_ORDER_STATUSES: frozenset[str] = frozenset(get_args(OrderStatus))


def _order_statuses(state: SqliteState, client_ids: Sequence[str | None]) -> dict[str, OrderStatus]:
    """Booked status per client id. Only known ``OrderStatus`` values are
    returned, so the caller falls back to the submission's own status."""
    ids = [c for c in client_ids if c]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = state.sql(
        f"SELECT client_id, status FROM orders WHERE client_id IN ({placeholders})", ids
    )
    return {r["client_id"]: r["status"] for r in rows if r["status"] in _ORDER_STATUSES}


def _live_order_status(state: SqliteState, client_id: str | None) -> str | None:
    """Status of an order already in the ledger that must not be placed
    again (anything but rejected/cancelled), or None when it may be placed."""
    rows = state.sql(
        "SELECT status FROM orders WHERE client_id = ? AND status NOT IN ('rejected', 'cancelled')",
        [client_id],
    )
    return rows[0]["status"] if rows else None


def _record_order(
    state: SqliteState,
    order: Order,
    status: OrderStatus,
    reason: str | None = None,
    portfolio_id: str | None = None,
) -> None:
    # ON CONFLICT DO UPDATE lets a retried tick progress the status of a
    # previously-``rejected`` order to ``filled`` if the re-submission
    # succeeds (clearing the old rejection reason). The WHERE guard avoids
    # rewriting ``updated_at`` when nothing changed (a retried tick
    # replaying an already-filled order). ``portfolio_id`` is written only
    # on insert (it is immutable).
    # The decision columns (BL-32) are written on insert only: a retried
    # order keeps the decision it was first placed with.
    now = _iso_now()
    extra: dict[str, Any] = {}
    if portfolio_id is not None:
        extra["portfolio_id"] = portfolio_id
    if tca_recorded(state):
        extra.update(decision_values(order))
    if order.position_effect is not None and "position_effect" in ledger_columns(state, "orders"):
        extra["position_effect"] = order.position_effect  # migration 021
    extra.update(order_live_values(order, ledger_columns(state, "orders")))  # migration 028
    has_state = "state" in ledger_columns(state, "orders")
    if has_state:
        # 19.8: the tick writes the fine state with the status (every
        # status is also a state). A re-record (a rerun resubmitting a
        # rejected order) starts the order's life again from that state.
        extra["state"] = status
    extra_col = "".join(f", {c}" for c in extra)
    extra_val = ", ?" * len(extra)
    reset_state = ",\n            state = excluded.state" if has_state else ""
    state.execute(
        f"""
        INSERT INTO orders
            (client_id, tick_id, strategy_id, ticker, side, quantity,
             order_type, limit_price, status, status_reason, broker_order_id,
             created_at, updated_at{extra_col})
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?{extra_val})
        ON CONFLICT (client_id) DO UPDATE SET
            status = excluded.status,
            status_reason = excluded.status_reason,
            updated_at = excluded.updated_at{reset_state}
          WHERE orders.status IS NOT excluded.status
             OR orders.status_reason IS NOT excluded.status_reason
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
            reason,
            now,
            now,
            *extra.values(),
        ],
    )


def _arrival(broker: Broker, fill: Fill) -> float | None:
    """The pre-cost price a simulated fill was priced from (the close it
    filled at): its arrival price for TCA. Unknown for other brokers."""
    reference = getattr(broker, "reference_price", None)
    if not callable(reference):
        return None
    value = reference(fill.order_client_id)
    return float(value) if isinstance(value, int | float) else None


def _record_fill(
    state: SqliteState,
    fill: Fill,
    portfolio_id: str | None = None,
    arrival_price: float | None = None,
) -> None:
    extra: dict[str, Any] = {}
    if portfolio_id is not None:
        extra["portfolio_id"] = portfolio_id
    if arrival_price is not None and tca_recorded(state):
        extra["arrival_price"] = arrival_price
    extra.update(fill_live_values(fill, ledger_columns(state, "fills")))  # migration 028
    extra_col = "".join(f", {c}" for c in extra)
    extra_val = ", ?" * len(extra)
    state.execute(
        f"""
        INSERT INTO fills
            (order_client_id, ticker, quantity, price, fee, filled_at{extra_col})
        VALUES (?, ?, ?, ?, ?, ?{extra_val})
        """,
        [
            fill.order_client_id,
            fill.ticker,
            fill.quantity,
            fill.price,
            fill.fee,
            fill.filled_at.isoformat(timespec="seconds"),
            *extra.values(),
        ],
    )


def _snapshot_portfolio(
    state: SqliteState,
    tick_id: str | None,
    portfolio: Portfolio,
    prices: dict[str, float],
    as_of: date,
    portfolio_id: str | None = None,
) -> None:
    total = portfolio.total_value(prices)
    extra_col, extra_val = (", portfolio_id", ", ?") if portfolio_id is not None else ("", "")
    state.execute(
        f"""
        INSERT INTO portfolio_snapshots
            (tick_id, as_of, taken_at, cash, positions_json, total_value{extra_col})
        VALUES (?, ?, ?, ?, ?, ?{extra_val})
        """,
        [
            tick_id,
            as_of.isoformat(),
            _iso_now(),
            portfolio.cash,
            json.dumps(portfolio.positions, sort_keys=True),
            total,
            *([portfolio_id] if portfolio_id is not None else []),
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
